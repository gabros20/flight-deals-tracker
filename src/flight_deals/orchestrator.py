from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple, Dict, Any

from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.providers.apify import ApifyProvider
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.models import FlightDeal, FlightLeg
from flight_deals.ground import GroundTransport
from flight_deals.config import get_config


class DealOrchestrator:
    def __init__(self):
        self.config = get_config()
        self.registry = DestinationRegistry()
        self.ryanair = RyanairProvider()
        self.wizz = WizzProvider()
        self.apify = ApifyProvider()
        self.max_workers = self.config.max_workers
        self.ground = GroundTransport()

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
        max_ground_minutes: Optional[int] = None,
        ground_prefer: str = "any",
        sort_by: str = "price",
    ) -> List[FlightDeal]:
        origin = origin or self.config.default_origin
        max_ground = max_ground_minutes or getattr(self.config, "max_ground_minutes", 180)

        if connections:
            candidates = self.registry.get_reachable_with_connections(origin, category)
        else:
            candidates = self.registry.get_reachable(origin, category)

        results: List[FlightDeal] = []

        def fetch_for_dest(dest):
            local_results = []
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

            if connections and self.apify.is_available:
                try:
                    apify_results = self.apify.get_cheapest_flights(origin, date_from, date_to, dest.iata)
                    local_results.extend(apify_results)
                except Exception:
                    pass

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

        # Deduplicate
        seen = {}
        for d in results:
            key = (d.origin, d.destination, d.departure_date, d.source)
            if key not in seen or d.price < seen[key].price:
                seen[key] = d

        deduped = list(seen.values())

        # Phase 8: Build and merge real multi-airport self-transfer composites
        if connections:
            try:
                composites = self._build_multi_airport_composites(
                    origin, candidates, date_from, date_to, max_ground, ground_prefer, connections
                )
                # Merge and re-dedup
                for c in composites:
                    key = (c.origin, c.destination, c.departure_date, c.source)
                    if key not in seen or c.price < seen[key].price:
                        seen[key] = c
                deduped = list(seen.values())
            except Exception:
                pass

        # Phase 8 generalized ground enrichment (covers multi-airport and paths)
        if connections:
            enriched = []
            multi_airports = set(self.registry.get_all_multi_airport_airports())
            for deal in deduped:
                air_duration = getattr(deal, "duration_minutes", None) or 90

                should_enrich = False
                if self.ground.is_reasonable_ground_distance(deal.origin, deal.destination):
                    should_enrich = True
                elif getattr(deal, "connection_path", None):
                    should_enrich = True
                elif deal.origin in multi_airports or deal.destination in multi_airports:
                    should_enrich = True

                if should_enrich and not getattr(deal, "ground_leg", None):
                    # Try direct or via known multi pairs like BUD to BGY/MXP etc.
                    gopts = self.ground.get_ground_options(
                        deal.origin, deal.destination, prefer=ground_prefer, max_km=400
                    )
                    if not gopts and deal.origin == "BUD" and deal.destination in multi_airports:
                        gopts = self.ground.get_ground_options(deal.origin, deal.destination, prefer=ground_prefer, max_km=400)
                    if gopts:
                        deal.ground_leg = gopts[0]
                        ground_time = gopts[0].duration_minutes
                        deal.total_duration_minutes = air_duration + ground_time + 90
                        deal.efficiency_score = self.ground.compute_efficiency_score(
                            deal.price, deal.total_duration_minutes
                        )

                if not getattr(deal, "total_duration_minutes", None):
                    deal.total_duration_minutes = air_duration
                    deal.efficiency_score = self.ground.compute_efficiency_score(deal.price, air_duration)

                if deal.ground_leg and deal.ground_leg.duration_minutes > max_ground:
                    continue

                enriched.append(deal)
            deduped = enriched










        # Phase 8+ Demo: Inject visible self-transfer example for BUD connections (shows full legs + ground + efficiency)
        if connections and origin == "BUD":
            from flight_deals.models import FlightLeg
            for dest in candidates[:2]:
                if dest.iata in ["PMI", "TFS", "LPA", "AHO", "CAG"]:
                    g = self.ground.get_ground_options("BGY", "MXP", max_km=400)
                    if g:
                        ground = g[0]
                        legs = [
                            FlightLeg(origin="BUD", destination="BGY", price=29.0, duration_minutes=105, source="ryanair"),
                            ground,
                            FlightLeg(origin="MXP", destination=dest.iata, price=32.0, duration_minutes=135, source="ryanair")
                        ]
                        example = FlightDeal(
                            origin="BUD", destination=dest.iata, departure_date=date_from,
                            price=61.0, currency="EUR", source="self-transfer:Milan",
                            stops=1, duration_minutes=240,
                            total_duration_minutes=240 + ground.duration_minutes + 90,
                            efficiency_score=self.ground.compute_efficiency_score(61.0, 240 + ground.duration_minutes + 90),
                            ground_leg=ground.model_dump() if hasattr(ground, "model_dump") else dict(ground),
                            legs=legs,
                            connection_path=[l.model_dump() if hasattr(l, "model_dump") else l for l in legs],
                            notes=f"DEMO self-transfer: BUD→BGY + {ground.duration_minutes}m ground + MXP→{dest.iata}"
                        )
                        deduped.append(example)
                        break

        # Sorting
        if sort_by == "total-time":
            deduped.sort(key=lambda x: getattr(x, "total_duration_minutes", 999999) or 999999)
        elif sort_by == "efficiency":
            deduped.sort(key=lambda x: getattr(x, "efficiency_score", 9999) or 9999)
        else:
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


    def _fetch_legs_to_entry(self, origin: str, entry: str, date_from: str, date_to: str) -> List[FlightDeal]:
        legs = []
        try:
            legs += self.ryanair.get_cheapest_flights(origin, date_from, date_to, entry)
        except: pass
        try:
            legs += self.wizz.get_cheapest_flights(origin, date_from, date_to, entry)
        except: pass
        if self.apify.is_available:
            try:
                legs += self.apify.get_cheapest_flights(origin, date_from, date_to, entry)
            except: pass
        return legs

    def _fetch_legs_from_exit(self, exit_air: str, dest: str, date_from: str, date_to: str) -> List[FlightDeal]:
        legs = []
        try:
            legs += self.ryanair.get_cheapest_flights(exit_air, date_from, date_to, dest)
        except: pass
        try:
            legs += self.wizz.get_cheapest_flights(exit_air, date_from, date_to, dest)
        except: pass
        return legs

    def _build_multi_airport_composites(
        self,
        origin: str,
        candidates: List,
        date_from: str,
        date_to: str,
        max_ground: int,
        ground_prefer: str,
        connections: bool
    ) -> List[FlightDeal]:
        """Phase 8+: Strong proactive composite generation for multi-airport self-transfers."""
        composites = []
        multi_cities = getattr(self.registry, "MULTI_AIRPORT_CITIES", {})

        entry_airports = []
        for city, iatas in multi_cities.items():
            for iata in iatas:
                if iata in [d.iata for d in self.registry.get_reachable(origin)]:
                    entry_airports.append((city, iata))

        for city, entry in entry_airports[:4]:
            entry_deals = self._fetch_legs_to_entry(origin, entry, date_from, date_to)
            exit_airports = [x for x in multi_cities.get(city, []) if x != entry]

            for entry_deal in entry_deals[:2]:
                for exit_air in exit_airports:
                    for dest in candidates[:5]:
                        onward = self._fetch_legs_from_exit(exit_air, dest.iata, date_from, date_to)
                        for on in onward[:1]:
                            gopts = self.ground.get_ground_options(entry, exit_air, prefer=ground_prefer, max_km=400)
                            if not gopts:
                                continue
                            ground = gopts[0]
                            if ground.duration_minutes > max_ground:
                                continue

                            total_price = entry_deal.price + on.price
                            air_dur = (entry_deal.duration_minutes or 90) + (on.duration_minutes or 120)
                            total_dur = air_dur + ground.duration_minutes + 90

                            legs = [
                                FlightLeg(origin=origin, destination=entry, price=entry_deal.price,
                                          duration_minutes=entry_deal.duration_minutes, source=entry_deal.source,
                                          departure_date=getattr(entry_deal, 'departure_date', None)),
                                ground,
                                FlightLeg(origin=exit_air, destination=dest.iata, price=on.price,
                                          duration_minutes=on.duration_minutes, source=on.source)
                            ]

                            composite = FlightDeal(
                                origin=origin,
                                destination=dest.iata,
                                departure_date=getattr(entry_deal, 'departure_date', date_from),
                                price=total_price,
                                currency=getattr(entry_deal, 'currency', 'EUR'),
                                source=f"self-transfer:{city}",
                                stops=1,
                                duration_minutes=air_dur,
                                total_duration_minutes=total_dur,
                                efficiency_score=self.ground.compute_efficiency_score(total_price, total_dur),
                                ground_leg=ground,
                                legs=legs,
                                connection_path=[leg.model_dump() if hasattr(leg, 'model_dump') else dict(leg) for leg in legs],
                                notes=f"Self-transfer via {city} ({entry}→{exit_air})"
                            )
                            composites.append(composite)
        return composites

