import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Dict, Any

from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.providers.apify import ApifyProvider
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.models import FlightDeal
from flight_deals.ground import GroundTransport
from flight_deals.config import get_config
from flight_deals.history import PriceHistoryStore

logger = logging.getLogger(__name__)


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
        # Per-provider health for the current/last search_by_category call,
        # printed by the CLI as a `sources:` line. Not a full status object
        # (that's the JSON envelope, Task 6) — just enough to stop "no
        # deals" and "provider is down" from looking identical.
        self.provider_status: Dict[str, Dict[str, Any]] = {}
        self._status_lock = threading.Lock()

    def _note_provider(self, name: str, ok: bool, error: Optional[str] = None) -> None:
        with self._status_lock:
            entry = self.provider_status.setdefault(
                name, {"ok": True, "calls": 0, "errors": 0, "last_error": None}
            )
            entry["calls"] += 1
            if not ok:
                entry["ok"] = False
                entry["errors"] += 1
                entry["last_error"] = error

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
        One-way search across the destinations reachable from `origin` for
        `category`. Round-trip and 1-stop composite search were removed
        pending rebuild (docs/UPGRADE-PLAN.md Phase 1/5) — see the CLI's
        `search` command, which refuses `--return-from/--return-to` and
        `--connections` before ever calling this.

        `connections=True` here only widens the candidate destination list
        and, if configured, adds Apify multi-source results; it does not
        build ground-transfer composites (that code never worked — it called
        a method that didn't exist and the AttributeError was swallowed).
        """
        origin = origin or self.config.default_origin
        self.provider_status = {}

        if connections:
            candidates = self.registry.get_reachable_with_connections(origin, category)
        else:
            candidates = self.registry.get_reachable(origin, category)

        results: List[FlightDeal] = []

        def fetch_for_dest(dest):
            local_results = []

            try:
                ryanair_out = self.ryanair.get_cheapest_flights(origin, date_from, date_to, dest.iata, use_cache=not fresh) or []
                local_results.extend(ryanair_out)
                self._note_provider("ryanair", self.ryanair.last_error is None, self.ryanair.last_error)
            except Exception as e:
                logger.warning("orchestrator: ryanair failed for %s->%s: %s", origin, dest.iata, e)
                self._note_provider("ryanair", False, str(e))

            try:
                wizz_out = self.wizz.get_cheapest_flights(origin, date_from, date_to, dest.iata, use_cache=not fresh) or []
                local_results.extend(wizz_out)
                self._note_provider("wizz", self.wizz.last_error is None, self.wizz.last_error)
            except Exception as e:
                logger.warning("orchestrator: wizz failed for %s->%s: %s", origin, dest.iata, e)
                self._note_provider("wizz", False, str(e))

            if connections and self.apify.is_available:
                try:
                    apify_results = self.apify.get_cheapest_flights(origin, date_from, date_to, dest.iata) or []
                    local_results.extend(apify_results)
                    self._note_provider("apify", self.apify.last_error is None, self.apify.last_error)
                except Exception as e:
                    logger.warning("orchestrator: apify failed for %s->%s: %s", origin, dest.iata, e)
                    self._note_provider("apify", False, str(e))

            return local_results

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_dest = {executor.submit(fetch_for_dest, dest): dest for dest in candidates}
            for future in as_completed(future_to_dest):
                dest = future_to_dest[future]
                try:
                    results.extend(future.result())
                except Exception as e:
                    logger.warning("orchestrator: destination worker failed for %s: %s", dest.iata, e)

        if max_price:
            results = [d for d in results if d.price <= max_price]

        # Deduplicate
        seen = {}
        for d in results:
            key = (d.origin, d.destination, d.departure_date, d.source)
            if key not in seen or d.price < seen[key].price:
                seen[key] = d

        deduped = list(seen.values())

        # Sorting
        if sort_by == "total-time":
            deduped.sort(key=lambda x: getattr(x, "total_duration_minutes", 999999) or 999999)
        elif sort_by == "efficiency":
            deduped.sort(key=lambda x: getattr(x, "efficiency_score", 9999) or 9999)
        else:
            deduped.sort(key=lambda x: x.price)

        # Enrich with historical price comparisons and badges (with window filtering)
        try:
            self.history.enrich_deals(deduped, window_days=history_window_days)
        except Exception as e:
            logger.warning("orchestrator: history enrichment failed: %s", e)

        return deduped
