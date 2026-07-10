"""
Wizz Air provider using the public timetable endpoint.

Prices from this endpoint are approximate (no per-date exact-price API is
used here); an estimate->confirm pipeline that only alerts on exact prices
is planned for a later phase (see docs/UPGRADE-PLAN.md Phase 1).
"""

import logging
import re
from typing import List, Optional

import requests

from flight_deals.cache import FlightCache
from flight_deals.models import FlightDeal

logger = logging.getLogger(__name__)


class WizzProvider:
    FALLBACK_VERSION = "27.13.0"

    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.session = requests.Session()
        self.name = "wizz"
        self.use_cache = use_cache
        self._cache = FlightCache() if use_cache else None
        self.last_error: Optional[str] = None
        self.version = self._get_current_version()

    def _get_current_version(self) -> str:
        """Try to detect the current Wizz API version from the website; fall back to a known-good pin."""
        try:
            resp = self.session.get("https://wizzair.com", timeout=10)
            match = re.search(r'be\.wizzair\.com/(\d+\.\d+\.\d+)', resp.text)
            if match:
                return match.group(1)
        except Exception as e:
            logger.warning("wizz: version discovery failed, using fallback %s: %s", self.FALLBACK_VERSION, e)
        return self.FALLBACK_VERSION

    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        """
        Return cheapest flights per day for the window. Returns [] (with
        ``self.last_error`` set) on any request/parse failure instead of
        raising, so a single provider outage doesn't take down a whole
        multi-destination search; callers should check ``last_error`` to
        distinguish "no deals" from "provider failed".
        """
        self.last_error = None

        if self._cache and use_cache:
            cached = self._cache.get(self.name, origin, date_from, date_to, destination_airport)
            if cached is not None:
                return cached

        url = f"https://be.wizzair.com/{self.version}/Api/search/timetable"
        payload = {
            "flightList": [
                {
                    "departureStation": origin,
                    "arrivalStation": destination_airport or "",
                    "from": date_from,
                    "to": date_to,
                }
            ],
            "priceType": "regular",
        }
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        }

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=20)
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}"
                logger.warning("wizz: %s returned %s", url, resp.status_code)
                return []
            data = resp.json()
        except Exception as e:
            self.last_error = str(e)
            logger.warning("wizz: request failed for %s->%s: %s", origin, destination_airport, e)
            return []

        try:
            deals: List[FlightDeal] = []
            for flight in data.get("outboundFlights", []):
                price_info = flight.get("price") or flight.get("fullBasePrice")
                if price_info and price_info.get("amount"):
                    deals.append(
                        FlightDeal(
                            origin=origin,
                            destination=flight.get("arrivalStation", destination_airport or ""),
                            departure_date=str(flight.get("departureDate", ""))[:10],
                            price=float(price_info["amount"]),
                            currency=price_info.get("currencyCode", self.currency),
                            source="wizz",
                        )
                    )
        except Exception as e:
            self.last_error = str(e)
            logger.warning("wizz: failed to parse response for %s->%s: %s", origin, destination_airport, e)
            return []

        if self._cache and deals:
            self._cache.set(self.name, origin, date_from, date_to, deals, destination_airport)

        return deals
