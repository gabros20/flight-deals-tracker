from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.providers.apify import ApifyProvider
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.models import FlightDeal
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from flight_deals.config import get_config
from flight_deals.ground import GroundTransport


class DealOrchestrator:
    def __init__(self, max_workers: Optional[int] = None):
        config = get_config()
        self.ryanair = RyanairProvider()
        self.wizz = WizzProvider()
        self.apify = ApifyProvider()
        self.registry = DestinationRegistry()
        self.max_workers = max_workers or config.max_workers

    def search_by_category(
        self,
        category: str,
        origin: str,
        date_from: str,
        date_to: str,
        max_price: Optional[int] = None,
        return_date_from: Optional[str] = None,
        return_date_to: Optional[str] = None,
        connections: bool = False,
    ) -> List[FlightDeal]:
        if connections:
            candidates = self.registry.get_reachable_with_connections(origin, category)
        else:
            candidates = self.registry.get_reachable(origin, category)

        results: List[FlightDeal] = []

        def fetch_for_dest(dest):
            local_results = []
            # Outbound direct (LCC)
            try:
                ryanair_out = self.ryanair.get_cheapest_flights(origin, date_from, date_to, dest.iata)
                local_results.extend(ryanair_out)
            except Exception:
                pass
            try:
                wizz_out = self.wizz.get_cheapest_flights(origin, date_from, date_to, dest.iata)
                local_results.extend(wizz_out)
            except Exception:
                pass

            # Apify for multi-source / connections
            if connections and self.apify.is_available:
                try:
                    apify_results = self.apify.get_cheapest_flights(origin, date_from, date_to, dest.iata)
                    local_results.extend(apify_results)
                except Exception:
                    pass

            # Return legs (LCC only for now)
            if return_date_from and return_date_to:
                try:
                    ryanair_ret = self.ryanair.get_cheapest_flights(dest.iata, return_date_from, return_date_to, origin)
                    local_results.extend(ryanair_ret)
                except Exception:
                    pass
                try:
                    wizz_ret = self.wizz.get_cheapest_flights(dest.iata, return_date_from, return_date_to, origin)
                    local_results.extend(wizz_ret)
                except Exception:
                    pass

                if connections and self.apify.is_available:
                    try:
                        apify_ret = self.apify.get_cheapest_flights(dest.iata, return_date_from, return_date_to, origin)
                        local_results.extend(apify_ret)
                    except Exception:
                        pass

            return local_results

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_dest = {executor.submit(fetch_for_dest, dest): dest for dest in candidates}
            for future in as_completed(future_to_dest):
                try:
                    results.extend(future.result())
                except Exception:
                    continue

        if max_price:
            results = [d for d in results if d.price <= max_price]

        # Deduplicate by key + keep cheapest
        seen = {}
        for d in results:
            key = (d.origin, d.destination, d.departure_date, d.source)
            if key not in seen or d.price < seen[key].price:
                seen[key] = d

        deduped = list(seen.values())
        deduped.sort(key=lambda x: x.price)
        return deduped

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
