from flight_deals.models import FlightDeal
from typing import List, Optional
import requests
import backoff
import re
from flight_deals.cache import FlightCache
from flight_deals.config import get_config


class WizzProvider:
    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.session = requests.Session()
        self.version = self._get_current_version()
        self.use_cache = use_cache
        self._cache = FlightCache() if use_cache else None
        self.name = "wizz"

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
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        # Check cache first
        if self._cache and use_cache:
            cached = self._cache.get(self.name, origin, date_from, date_to, destination_airport)
            if cached is not None:
                return cached

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

            # Store in cache
            if self._cache and deals:
                self._cache.set(self.name, origin, date_from, date_to, deals, destination_airport)

            return deals
        except Exception:
            return []

                
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
