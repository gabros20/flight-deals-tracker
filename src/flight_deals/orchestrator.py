from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.models import FlightDeal
from typing import List, Optional, Tuple


class DealOrchestrator:
    def __init__(self):
        self.ryanair = RyanairProvider()
        self.wizz = WizzProvider()
        self.registry = DestinationRegistry()

    def search_by_category(
        self,
        category: str,
        origin: str,
        date_from: str,
        date_to: str,
        max_price: Optional[int] = None,
        return_date_from: Optional[str] = None,
        return_date_to: Optional[str] = None,
    ) -> List[FlightDeal]:
        candidates = self.registry.get_by_tag(category)
        results: List[FlightDeal] = []

        for dest in candidates:
            # Outbound
            ryanair_out = self.ryanair.get_cheapest_flights(origin, date_from, date_to, dest.iata)
            wizz_out = self.wizz.get_cheapest_flights(origin, date_from, date_to, dest.iata)
            results.extend(ryanair_out + wizz_out)

            # Return (if requested)
            if return_date_from and return_date_to:
                ryanair_ret = self.ryanair.get_cheapest_flights(dest.iata, return_date_from, return_date_to, origin)
                wizz_ret = self.wizz.get_cheapest_flights(dest.iata, return_date_from, return_date_to, origin)
                results.extend(ryanair_ret + wizz_ret)

        if max_price:
            results = [d for d in results if d.price <= max_price]

        results.sort(key=lambda x: x.price)
        return results

    def find_roundtrip_deals(
        self,
        origin: str,
        destination: str,
        outbound_from: str,
        outbound_to: str,
        return_from: str,
        return_to: str,
        max_price: Optional[int] = None,
    ) -> List[Tuple[FlightDeal, FlightDeal]]:
        """Find paired round-trip deals"""
        out_deals = self.ryanair.get_cheapest_flights(origin, outbound_from, outbound_to, destination)
        out_deals += self.wizz.get_cheapest_flights(origin, outbound_from, outbound_to, destination)

        ret_deals = self.ryanair.get_cheapest_flights(destination, return_from, return_to, origin)
        ret_deals += self.wizz.get_cheapest_flights(destination, return_from, return_to, origin)

        roundtrips = []
        for out in out_deals:
            for ret in ret_deals:
                total = out.price + ret.price
                if max_price is None or total <= max_price:
                    roundtrips.append((out, ret))

        roundtrips.sort(key=lambda x: x[0].price + x[1].price)
        return roundtrips[:10]