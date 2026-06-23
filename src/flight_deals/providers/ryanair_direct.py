"""
Ryanair Provider using stable farfnd/v4/roundTripFares (migrated from brittle availability endpoint).
Folded in patterns from @2bad/ryanair (client-version), ryanair-py examples, and farfnd usage in community projects (sahibammar, farepy notes).
This provides reliable round-trip prices for short stays from BUD.
"""

import requests
from datetime import date
from typing import Optional, Dict, Any
import logging
import time

logger = logging.getLogger(__name__)

class RyanairDirectProvider:
    """Uses Ryanair's public farfnd/v4/roundTripFares for reliable round-trips."""

    FARFND_URL = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"

    def get_roundtrip_price(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date,
        adults: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Get round-trip price using the stable farfnd endpoint."""

        time.sleep(2)  # Respect rate limits (~1 req / few seconds per research)

        params = {
            "departureAirportIataCode": origin,
            "arrivalAirportIataCode": destination,
            "outboundDepartureDateFrom": departure_date.isoformat(),
            "outboundDepartureDateTo": departure_date.isoformat(),
            "inboundDepartureDateFrom": return_date.isoformat(),
            "inboundDepartureDateTo": return_date.isoformat(),
            "currency": "EUR",
            "market": "en-gb",
            "adults": adults,
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "Referer": "https://www.ryanair.com/",
        }

        try:
            response = requests.get(self.FARFND_URL, params=params, headers=headers, timeout=15)

            if response.status_code != 200:
                logger.warning(f"farfnd returned {response.status_code}")
                return None

            data = response.json()
            fares = data.get("fares", [])

            if not fares:
                return None

            # Take the cheapest available fare for the exact dates
            fare = fares[0]
            outbound_price = fare.get("outbound", {}).get("price", {})
            inbound_price = fare.get("inbound", {}).get("price", {}) if fare.get("inbound") else {}

            # Total price
            total = 0.0
            if outbound_price.get("value"):
                total += float(outbound_price["value"])
            if inbound_price.get("value"):
                total += float(inbound_price["value"])

            if total == 0:
                return None

            currency = outbound_price.get("currencyCode", "EUR")

            return {
                "price": round(total, 2),
                "currency": currency,
                "source": "ryanair-farfnd",
                "outbound_date": departure_date.isoformat(),
                "return_date": return_date.isoformat(),
            }

        except Exception as e:
            logger.error(f"Ryanair farfnd API failed: {e}")
            return None

    def get_cheapest_in_range(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        return_start: date,
        return_end: date,
        adults: int = 1,
    ) -> list:
        """For broader searches - returns list of fares in date range (inspired by ryanair-py and 2bad)."""
        time.sleep(2)
        params = {
            "departureAirportIataCode": origin,
            "arrivalAirportIataCode": destination,
            "outboundDepartureDateFrom": start_date.isoformat(),
            "outboundDepartureDateTo": end_date.isoformat(),
            "inboundDepartureDateFrom": return_start.isoformat(),
            "inboundDepartureDateTo": return_end.isoformat(),
            "currency": "EUR",
            "market": "en-gb",
            "adults": adults,
        }
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

        try:
            resp = requests.get(self.FARFND_URL, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for fare in data.get("fares", [])[:10]:  # limit
                out_price = fare.get("outbound", {}).get("price", {}).get("value", 0)
                in_price = fare.get("inbound", {}).get("price", {}).get("value", 0) if fare.get("inbound") else 0
                total = round(float(out_price) + float(in_price), 2)
                if total > 0:
                    results.append({
                        "price": total,
                        "currency": "EUR",
                        "outbound_date": fare.get("outbound", {}).get("departureDate"),
                        "return_date": fare.get("inbound", {}).get("departureDate") if fare.get("inbound") else None,
                        "source": "ryanair-farfnd",
                    })
            return results
        except Exception as e:
            logger.error(f"farfnd range failed: {e}")
            return []
