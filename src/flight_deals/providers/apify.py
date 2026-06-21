"""
Apify Multi-Source Provider
Uses the cheapest multi-airline actor for connections and broader coverage.
Actor: makework36/flight-price-scraper (Google Flights + Kiwi + LCCs)
"""

import requests
import backoff
from typing import List, Optional, Dict, Any
from flight_deals.models import FlightDeal
from flight_deals.config import FlightDealsConfig, get_config
from flight_deals.cache import FlightCache


class ApifyProvider:
    def __init__(self, config: Optional[FlightDealsConfig] = None, use_cache: bool = True):
        self.config = config or get_config()
        self.use_cache = use_cache
        self._cache = FlightCache() if use_cache else None
        self.name = "apify"
        self.actor_id = self.config.apify_actor_id
        self.base_url = "https://api.apify.com/v2"

    @property
    def is_available(self) -> bool:
        return self.config.has_apify

    def _build_input(self, origin: str, date_from: str, date_to: str,
                     destination_airport: Optional[str] = None) -> Dict[str, Any]:
        """Build input for the flight price scraper actor."""
        # The actor supports direct route + date queries.
        # For connections we rely on Google Flights / Kiwi results inside it.
        input_data = {
            "origin": origin,
            "dateFrom": date_from,
            "dateTo": date_to,
            "maxFlights": 50,
            "currency": self.config.currency,
        }
        if destination_airport:
            input_data["destination"] = destination_airport
        return input_data

    @backoff.on_exception(backoff.expo, Exception, max_tries=2)
    def _call_apify(self, input_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.is_available:
            return []

        token = self.config.apify_token
        url = f"{self.base_url}/acts/{self.actor_id}/run-sync-get-dataset-items"
        headers = {"Authorization": f"Bearer {token}"}

        response = requests.post(
            url,
            headers=headers,
            json={"input": input_data},
            timeout=30
        )
        response.raise_for_status()
        return response.json() or []

    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
    ) -> List[FlightDeal]:
        if not self.is_available:
            return []

        # Cache key uses "apify" namespace
        if self._cache:
            cached = self._cache.get(
                "apify", origin, date_from, date_to, destination_airport
            )
            if cached is not None:
                return cached

        try:
            input_data = self._build_input(origin, date_from, date_to, destination_airport)
            raw_results = self._call_apify(input_data)

            deals: List[FlightDeal] = []
            for item in raw_results:
                price = item.get("bestPrice") or item.get("price")
                if not price:
                    continue

                source = item.get("cheapestSource", "apify")
                source_label = f"apify:{source}"

                # Count stops from segments if present
                segments = item.get("segments", []) or []
                stops = max(0, len(segments) - 1) if segments else 0

                deal = FlightDeal(
                    origin=origin,
                    destination=destination_airport or item.get("destination", ""),
                    departure_date=date_from,  # best approximation; real date in full item if needed
                    price=float(price),
                    currency=item.get("currency", self.config.currency),
                    source=source_label,
                    stops=stops,
                    source_details={
                        "cheapestSource": source,
                        "prices": item.get("prices", {}),
                        "isSelfTransfer": item.get("isSelfTransfer", False),
                    },
                    booking_url=item.get("bookingLinks", {}).get(source) or item.get("bookingUrl"),
                )
                deals.append(deal)

            if self._cache and deals:
                self._cache.set("apify", origin, date_from, date_to, destination_airport, deals)

            return deals

        except Exception as e:
            # Fail silently for cost and UX reasons; log in real use
            print(f"[ApifyProvider] Warning: {e}")
            return []
