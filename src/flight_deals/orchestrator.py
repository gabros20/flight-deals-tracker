import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.providers.apify import ApifyProvider
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.models import FlightDeal
from flight_deals.ground import GroundTransport
from flight_deals.config import get_config
from flight_deals.history import PriceHistoryStore
from flight_deals import http
from flight_deals.http import Blocked, ProviderDown, ProviderError, RateLimited, SchemaError

logger = logging.getLogger(__name__)


def status_for_exception(exc: BaseException) -> str:
    """
    Map a typed provider/HTTP exception to a frozen ``sources`` status
    (docs/CONTRACT.md §1). Anything unexpected degrades to generic ``error`` —
    never silently to "ok"/"no results".
    """
    if isinstance(exc, (RateLimited, Blocked)):
        return "blocked"
    if isinstance(exc, SchemaError):
        return "parse_error"
    if isinstance(exc, ProviderDown):
        return "error"
    return "error"


def aggregate_status(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Merge per-call status events (each produced *locally* by the worker that
    made the call — never read from a shared, mutable provider attribute) into a
    per-provider summary. Merging happens single-threaded in the caller, so a
    failure in one of N concurrent workers can never be lost to a race
    (Task 3 req 8). "Worst status wins": any non-ok event marks the provider
    failed and records its detail.
    """
    summary: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        name = ev["provider"]
        entry = summary.setdefault(
            name, {"ok": True, "status": "ok", "calls": 0, "errors": 0, "last_error": None}
        )
        entry["calls"] += 1
        if ev["status"] != "ok":
            entry["ok"] = False
            entry["errors"] += 1
            entry["status"] = ev["status"]
            entry["last_error"] = ev.get("detail")
    return summary


class DealOrchestrator:
    def __init__(self):
        self.config = get_config()
        self.registry = DestinationRegistry()
        self.ryanair = RyanairProvider()
        self.wizz = WizzProvider()
        self.apify = ApifyProvider()
        self.max_workers = self.config.max_workers
        self.ground = GroundTransport()
        self.history = PriceHistoryStore()
        # Aligns the shared rate limiter with configured policy (Constraint 9).
        http.set_rate(self.config.http_rate_per_second)
        # Per-provider health for the last search, printed by the CLI as a
        # `sources:` line. Populated race-free from returned events, not by
        # threads writing a shared dict.
        self.provider_status: Dict[str, Dict[str, Any]] = {}
        # Wizz is still the legacy []+last_error provider (Task 4 rebuilds it).
        # A single shared instance's last_error races under concurrency, so its
        # call+read is serialized here until the rebuild.
        self._wizz_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Race-free concurrent gather                                        #
    # ------------------------------------------------------------------ #
    def _gather(self, items, worker) -> Tuple[List[FlightDeal], List[Dict[str, Any]]]:
        """
        Run ``worker(item)`` concurrently under the shared rate limiter. Each
        worker returns ``(deals, events)`` where ``events`` are its OWN status
        observations. This method merges them single-threaded — no worker reads
        another worker's status.
        """
        results: List[FlightDeal] = []
        events: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(worker, it): it for it in items}
            for future in as_completed(futures):
                it = futures[future]
                try:
                    deals, evs = future.result()
                    results.extend(deals)
                    events.extend(evs)
                except Exception as e:  # a worker must never crash the sweep
                    logger.warning("orchestrator: worker failed for %s: %s", it, e)
                    events.append({"provider": "orchestrator", "status": "error", "detail": str(e)})
        return results, events

    def _ryanair_oneway(self, origin, dest, date_from, date_to, fresh) -> Tuple[List[FlightDeal], Dict[str, Any]]:
        try:
            deals = self.ryanair.get_cheapest_flights(
                origin, date_from, date_to, dest.iata, use_cache=not fresh
            )
            return deals, {"provider": "ryanair", "status": "ok"}
        except ProviderError as e:
            logger.warning("orchestrator: ryanair %s->%s failed: %s", origin, dest.iata, e)
            return [], {"provider": "ryanair", "status": status_for_exception(e), "detail": str(e)}
        except Exception as e:
            logger.warning("orchestrator: ryanair %s->%s unexpected: %s", origin, dest.iata, e)
            return [], {"provider": "ryanair", "status": "error", "detail": str(e)}

    def _wizz_oneway(self, origin, dest, date_from, date_to, fresh) -> Tuple[List[FlightDeal], Dict[str, Any]]:
        # Legacy wizz: serialize the call+last_error read so the shared instance
        # can't have its last_error clobbered by another thread mid-read.
        with self._wizz_lock:
            try:
                raw = self.wizz.get_cheapest_flights(
                    origin, date_from, date_to, dest.iata, use_cache=not fresh
                ) or []
                err = getattr(self.wizz, "last_error", None)
            except Exception as e:
                return [], {"provider": "wizz", "status": "error", "detail": str(e)}
        if err:
            return [], {"provider": "wizz", "status": "error", "detail": err}
        # fx is Task 4: nothing non-EUR is allowed into results/stats
        # (Global Constraint 4). Drop non-EUR wizz deals rather than mix
        # currencies; wizz becomes fully usable once fx.py lands.
        eur = [d for d in raw if (d.currency or "EUR").upper() == "EUR"]
        if len(eur) != len(raw):
            logger.info("orchestrator: dropped %d non-EUR wizz deals (fx is Task 4)", len(raw) - len(eur))
        return eur, {"provider": "wizz", "status": "ok"}

    def search_by_category(
        self,
        category: str,
        origin: str,
        date_from: str,
        date_to: str,
        max_price: Optional[int] = None,
        connections: bool = False,
        sort_by: str = "price",
        history_window_days: int = None,
        fresh: bool = False,
    ) -> List[FlightDeal]:
        """
        One-way search across destinations reachable from ``origin`` for
        ``category``. Round-trip / connection composites were removed pending
        rebuild (the CLI `search` refuses them before calling this).
        """
        origin = origin or self.config.default_origin
        candidates = self.registry.get_reachable(origin, category)

        def worker(dest):
            deals: List[FlightDeal] = []
            events: List[Dict[str, Any]] = []

            r_deals, r_ev = self._ryanair_oneway(origin, dest, date_from, date_to, fresh)
            deals.extend(r_deals)
            events.append(r_ev)

            w_deals, w_ev = self._wizz_oneway(origin, dest, date_from, date_to, fresh)
            deals.extend(w_deals)
            events.append(w_ev)

            return deals, events

        results, events = self._gather(candidates, worker)
        self.provider_status = aggregate_status(events)

        if max_price:
            results = [d for d in results if d.price <= max_price]

        # Deduplicate: keep cheapest per (origin, dest, date, source)
        seen: Dict[tuple, FlightDeal] = {}
        for d in results:
            key = (d.origin, d.destination, d.departure_date, d.source)
            if key not in seen or d.price < seen[key].price:
                seen[key] = d
        deduped = list(seen.values())

        if sort_by == "total-time":
            deduped.sort(key=lambda x: getattr(x, "total_duration_minutes", 999999) or 999999)
        elif sort_by == "efficiency":
            deduped.sort(key=lambda x: getattr(x, "efficiency_score", 9999) or 9999)
        else:
            deduped.sort(key=lambda x: x.price)

        try:
            self.history.enrich_deals(deduped, window_days=history_window_days)
        except Exception as e:
            logger.warning("orchestrator: history enrichment failed: %s", e)

        return deduped
