"""
One-way Ryanair provider, currently backed by the third-party `ryanair-py`
package.

`ryanair-py` is intentionally NOT a declared dependency in pyproject.toml
(see docs/UPGRADE-PLAN.md §7/§9 — Phase 1 rebuilds this on the same farfnd
endpoint `providers/ryanair_direct.py` already uses, dropping the third-party
client entirely). The import below is therefore optional: if `ryanair-py`
isn't installed, this provider reports itself unavailable via `last_error`
instead of crashing the whole CLI at import time.
"""

import logging
from typing import List, Optional

from flight_deals.models import FlightDeal
from flight_deals.cache import FlightCache

logger = logging.getLogger(__name__)

try:
    from ryanair import Ryanair as RyanairClient
except ImportError:  # ryanair-py not installed — provider degrades gracefully
    RyanairClient = None
    logger.warning("ryanair-py not installed; RyanairProvider will report unavailable (see docs/UPGRADE-PLAN.md Phase 1)")


class RyanairProvider:
    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.client = RyanairClient(currency=currency) if RyanairClient else None
        self.use_cache = use_cache
        self._cache = FlightCache() if use_cache else None
        self.name = "ryanair"
        self.last_error: Optional[str] = None

    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        """
        Return cheapest one-way flights per day for the window. Returns []
        (with ``self.last_error`` set) on any failure instead of raising, so
        one provider outage doesn't take down a whole multi-destination
        search; callers should check ``last_error`` to distinguish "no
        deals" from "provider failed".
        """
        self.last_error = None

        if self._cache and use_cache:
            cached = self._cache.get(self.name, origin, date_from, date_to, destination_airport)
            if cached is not None:
                return cached

        if self.client is None:
            self.last_error = "ryanair-py not installed"
            logger.warning("ryanair: skipping %s->%s, ryanair-py not installed", origin, destination_airport)
            return []

        try:
            flights = self.client.get_cheapest_flights(
                airport=origin,
                date_from=date_from,
                date_to=date_to,
                destination_airport=destination_airport,
            )
            deals = [
                FlightDeal(
                    origin=f.origin,
                    destination=f.destination,
                    departure_date=f.departureTime.date().isoformat(),
                    price=f.price,
                    currency=f.currency,
                    source="ryanair",
                    flight_number=f.flightNumber,
                )
                for f in flights
            ]
        except Exception as e:
            self.last_error = str(e)
            logger.warning("ryanair: request failed for %s->%s: %s", origin, destination_airport, e)
            return []

        if self._cache and deals:
            self._cache.set(self.name, origin, date_from, date_to, deals, destination_airport)

        return deals
