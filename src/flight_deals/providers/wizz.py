from flight_deals.models import FlightDeal
from typing import List, Optional
import requests
import backoff
import re


class WizzProvider:
    def __init__(self, currency: str = "EUR"):
        self.currency = currency
        self.session = requests.Session()
        self.version = self._get_current_version()

    def _get_current_version(self) -> str:
        """Try to detect current Wizz API version from the website"""
        try:
            resp = self.session.get("https://wizzair.com", timeout=10)
            match = re.search(r'be\.wizzair\.com/(\d+\.\d+\.\d+)', resp.text)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "27.13.0"  # Fallback version

    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
    ) -> List[FlightDeal]:
        try:
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
            resp = self.session.post(url, json=payload, headers=headers, timeout=20)
            if resp.status_code != 200:
                return []

            data = resp.json()
            deals = []
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
            return deals
        except Exception:
            return []