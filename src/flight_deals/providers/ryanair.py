from ryanair import Ryanair as RyanairClient
from flight_deals.models import FlightDeal
from typing import List, Optional
import backoff
from datetime import datetime


class RyanairProvider:
    def __init__(self, currency: str = "EUR"):
        self.currency = currency
        self.client = RyanairClient(currency=currency)

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
    ) -> List[FlightDeal]:
        try:
            flights = self.client.get_cheapest_flights(
                airport=origin,
                date_from=date_from,
                date_to=date_to,
                destination_airport=destination_airport,
            )
            return [
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
        except Exception:
            return []