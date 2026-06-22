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

    def get_roundtrip_price(self, origin: str, destination: str, 
                            outbound_from: str, outbound_to: str,
                            return_from: str, return_to: str,
                            use_cache: bool = True) -> Optional[dict]:
        """Get cheapest round-trip price by combining outbound + return"""
        try:
            outbound = self.get_cheapest_flights(origin, outbound_from, outbound_to, destination, use_cache=use_cache)
            if not outbound:
                return None
                
            return_legs = self.get_cheapest_flights(destination, return_from, return_to, origin, use_cache=use_cache)
            if not return_legs:
                return None

            cheapest_out = min(outbound, key=lambda x: x.price)
            cheapest_ret = min(return_legs, key=lambda x: x.price)
            
            return {
                "total_price": cheapest_out.price + cheapest_ret.price,
                "currency": cheapest_out.currency,
                "outbound_price": cheapest_out.price,
                "return_price": cheapest_ret.price,
                "outbound_date": cheapest_out.departure_date,
                "return_date": cheapest_ret.departure_date,
            }
        except Exception:
            return None
