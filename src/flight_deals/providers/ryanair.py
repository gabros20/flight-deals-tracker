from ryanair import Ryanair as RyanairClient
from flight_deals.models import FlightDeal
from typing import List, Optional
import backoff
from datetime import datetime
from flight_deals.cache import FlightCache
from flight_deals.config import get_config


class RyanairProvider:
    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.client = RyanairClient(currency=currency)
        self.use_cache = use_cache
        self._cache = FlightCache() if use_cache else None
        self.name = "ryanair"

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        # Check cache first
        if self._cache and use_cache:
            cached = self._cache.get(self.name, origin, date_from, date_to, destination_airport)
            if cached is not None:
                return cached

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

            # Store in cache
            if self._cache and deals:
                self._cache.set(self.name, origin, date_from, date_to, deals, destination_airport)

            return deals
        except Exception:
            return []