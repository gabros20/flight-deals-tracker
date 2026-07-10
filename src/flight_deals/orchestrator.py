import logging
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


def _tie_break_key(d: FlightDeal) -> Tuple[bool, str]:
    """
    Deterministic ordering used ONLY to break an exact price tie in the
    cross-carrier merge below (Task 4 fix). Ryanair's `cheapestPerDay` is an
    exact fare (no `price_confidence` in `source_details`); Wizz's timetable
    is `approximate` (+-10%). On equal price, an exact fare must win over an
    approximate one; if that's still tied, fall back to `source` so the result
    never depends on thread completion order.
    """
    is_approximate = d.source_details.get("price_confidence") == "approximate"
    return (is_approximate, d.source)


def merge_cross_carrier(deals: List[FlightDeal]) -> List[FlightDeal]:
    """
    Merge across carriers: for one route+date served by both Ryanair and Wizz,
    keep the CHEAPER fare (its `source` tags the winning carrier) rather than
    showing near-duplicate rows (Task 4 req 4). All prices are EUR by here
    (Wizz went through fx.to_eur), so the comparison is currency-safe.

    A standalone, order-independent function (not inlined in the caller) so
    it can be exercised directly with `deals` in either order: on an exact
    price TIE, the winner is decided purely by `(price, _tie_break_key)` —
    exact confidence beats approximate, then `source` lexicographically —
    NEVER by which element happened to come first in the input list (which,
    inlined, would silently depend on `ThreadPoolExecutor`/`as_completed`
    completion order across workers).
    """
    seen: Dict[tuple, FlightDeal] = {}
    for d in deals:
        key = (d.origin, d.destination, d.departure_date)
        existing = seen.get(key)
        if existing is None or (d.price, _tie_break_key(d)) < (existing.price, _tie_break_key(existing)):
            seen[key] = d
    return list(seen.values())


def aggregate_status(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Merge per-call status events (each produced *locally* by the worker that
    made the call — never read from a shared, mutable provider attribute) into a
    per-provider summary. Merging happens single-threaded in the caller, so a
    failure in one of N concurrent workers can never be lost to a race
    (Task 3 req 8). "Worst status wins": any non-ok event marks the provider
    failed and records its detail.
    """
    ok_statuses = {"ok", "version_refreshed"}
    summary: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        name = ev["provider"]
        entry = summary.setdefault(
            name, {"ok": True, "status": "ok", "calls": 0, "errors": 0, "last_error": None}
        )
        entry["calls"] += 1
        st = ev["status"]
        if st not in ok_statuses:
            # A real failure wins over everything (incl. a version_refreshed).
            entry["ok"] = False
            entry["errors"] += 1
            entry["status"] = st
            entry["last_error"] = ev.get("detail")
        elif st == "version_refreshed" and entry["ok"] and entry["status"] == "ok":
            # Successful, but note the auto version refresh (Task 4) unless a
            # failure has already been recorded for this provider.
            entry["status"] = "version_refreshed"
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
        # Rebuilt Wizz (Task 4): typed exceptions out, prices already EUR via
        # fx.to_eur, and the version-refresh flag returned per-call (never shared
        # mutable state) so `version_refreshed` is reported race-free.
        try:
            deals, refreshed = self.wizz.oneway_deals(
                origin, dest.iata, date_from, date_to, use_cache=not fresh
            )
            return deals, {"provider": "wizz", "status": "version_refreshed" if refreshed else "ok"}
        except ProviderError as e:
            logger.warning("orchestrator: wizz %s->%s failed: %s", origin, dest.iata, e)
            return [], {"provider": "wizz", "status": status_for_exception(e), "detail": str(e)}
        except Exception as e:
            logger.warning("orchestrator: wizz %s->%s unexpected: %s", origin, dest.iata, e)
            return [], {"provider": "wizz", "status": "error", "detail": str(e)}

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

        # Cross-carrier merge/tie-break lives in `merge_cross_carrier` (Task 4
        # fix) so it's order-independent by construction and directly testable
        # in both insertion orders — see tests/test_orchestrator.py.
        deduped = merge_cross_carrier(results)

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
