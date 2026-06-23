"""
Farepy Provider - Multi-source round-trip support (including Ryanair + Google Flights)
This is the primary fix for reliable round-trip pricing.
"""

from datetime import date
from typing import Optional, Dict, Any
import logging

try:
    from farepy import search_flights
except ImportError:
    search_flights = None

logger = logging.getLogger(__name__)


class FarepyProvider:
    """Provider using farepy for robust round-trip searches."""

    def __init__(self, currency: str = "EUR"):
        self.currency = currency
        if search_flights is None:
            logger.warning("farepy not installed. Run: pip install farepy[google_flights,ryanair]")

    def get_roundtrip_price(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date,
        adults: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        Get round-trip price using farepy.
        Returns normalized offer or None if no results.
        """
        if search_flights is None:
            return None

        try:
            route = f"{origin}-{destination}"
            result = search_flights(
                route,
                departure_date.strftime("%Y-%m-%d"),
                sources=["ryanair", "google_flights"],
                return_date=return_date.strftime("%Y-%m-%d"),
            )

            if not result or not result.get("offers"):
                return None

            # Take the cheapest offer
            best = sorted(result["offers"], key=lambda x: x.get("price", 9999))[0]

            return {
                "price": best.get("price"),
                "currency": best.get("currency", self.currency),
                "source": best.get("cheapest_source", "farepy"),
                "outbound": best.get("outbound"),
                "inbound": best.get("inbound"),
                "booking_url": best.get("booking_url"),
            }

        except Exception as e:
            logger.error(f"Farepy roundtrip search failed: {e}")
            return None
