import json
import logging
from typing import List, Optional, Set, Dict
from flight_deals.models import Airport
from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)


# Known direct connections from popular Ryanair & Wizz bases (easily extendable)
# Format: origin -> set of common direct destinations
# Sources: Common routes from airline route maps (updated with 2026 data)
KNOWN_DIRECT_ROUTES: Dict[str, Set[str]] = {
    # BUD (Budapest) - primary for user - very rich coverage
    "BUD": {
        "PMI", "IBZ", "MAH", "CFU", "HER", "CHQ", "ZTH", "JTR", "RHO", "PVK", "KLX", "EFL", "JMK", "SKG",
        "CTA", "PMO", "CAG", "OLB", "AHO",
        "ALC", "AGP", "FAO", "VLC", "GRO", "CDT",
        "LPA", "TFS", "FUE", "FNC",
        "BRI", "SUF", "BDS", "NAP", "VCE", "PSA",
        "DBV", "ZAD", "SPU",
        "MLA",
        "LIS", "OPO",
        "BGY", "BCN", "MAD", "STN", "EDI", "PRG",
        "DUB", "VIE", "MUC", "FRA",
        "BOJ", "VAR", "TIA"
    },
    # Other bases for completeness
    "STN": {"PMI", "IBZ", "CFU", "HER", "CTA", "PMO", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN", "MAD"},
    "BGY": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "BRI", "SUF", "BDS"},
    "DUB": {"PMI", "IBZ", "CFU", "HER", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN"},
    "VIE": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "ALC", "AGP", "FAO", "BRI", "SUF"},
    "EDI": {"PMI", "IBZ", "CFU", "HER", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN", "MAD", "BUD"},
    "BCN": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "BRI", "SUF", "BDS", "BUD", "VIE"},
    "MAD": {"PMI", "IBZ", "CFU", "HER", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "BUD"},
}

# Popular hubs for 1-stop connections from BUD (good for extending reach with Ryanair/Wizz or other carriers)

# Multi-airport cities excellent for self-transfers / flight changes
# These allow flying into one airport and ground transferring to another in the same city
# Very useful for LCC connections (Ryanair/Wizz/Pegasus bases in different airports)
MULTI_AIRPORT_CITIES: Dict[str, List[str]] = {
    "Istanbul": ["IST", "SAW"],
    "London": ["STN", "LGW", "LTN"],
    "Milan": ["BGY", "MXP"],
    "Rome": ["CIA", "FCO"],
    "Paris": ["BVA", "CDG"],
    "Brussels": ["CRL", "BRU"],
    "Warsaw": ["WAW", "WMI"],
}

CONNECTION_HUBS: Dict[str, List[str]] = {
    "BUD": ["VIE", "MUC", "FRA", "AMS", "CDG", "IST", "PRG"]  # Common good connection points
}


class DestinationRegistry:
    def __init__(self, data_path: Optional[str] = None):
        self.data_path = resolve_path(data_path or "data/destinations.json")
        self.airports: List[Airport] = []
        self._load()

    def _load(self):
        if self.data_path.exists():
            data = json.loads(self.data_path.read_text())
            self.airports = [Airport(**item) for item in data]
        else:
            logger.warning("registry: destinations file not found at %s; registry will be empty", self.data_path)

    def get_by_tag(self, tag: str) -> List[Airport]:
        return [a for a in self.airports if tag in a.tags]

    def get_by_origin(self, origin_iata: str) -> List[Airport]:
        return [a for a in self.airports if a.iata != origin_iata]

    def get_reachable(self, origin: str, category: Optional[str] = None) -> List[Airport]:
        """
        Return destinations that are likely reachable DIRECT from the given origin.
        Uses known direct routes + falls back to all if unknown.
        """
        candidates = self.get_by_tag(category) if category else self.airports
        candidates = [a for a in candidates if a.iata != origin]

        known = KNOWN_DIRECT_ROUTES.get(origin.upper(), set())
        if known:
            reachable = [a for a in candidates if a.iata in known]
            if reachable:
                return reachable

        return candidates

    def get_all_tags(self) -> Set[str]:
        tags: Set[str] = set()
        for a in self.airports:
            tags.update(a.tags)
        return tags


    def get_multi_airport_cities(self) -> List[str]:
        """Return list of cities that have multiple useful airports for self-transfers."""
        return list(MULTI_AIRPORT_CITIES.keys())

    def get_airports_for_multi_city(self, city: str) -> List[str]:
        """Return all IATA codes for a multi-airport city."""
        return MULTI_AIRPORT_CITIES.get(city, [])

    def get_all_multi_airport_airports(self) -> List[str]:
        """Flat list of all airports that are part of a multi-airport city."""
        all_iatas = []
        for airports in MULTI_AIRPORT_CITIES.values():
            all_iatas.extend(airports)
        return all_iatas

    def get_ground_transfer_pairs(self) -> List[tuple]:
        """Return pairs of airports within the same multi-airport city for ground transfer calculation."""
        pairs = []
        for city, iatas in MULTI_AIRPORT_CITIES.items():
            for i in range(len(iatas)):
                for j in range(i+1, len(iatas)):
                    pairs.append((iatas[i], iatas[j]))
                    pairs.append((iatas[j], iatas[i]))  # both directions
        return pairs

    def get_reachable_with_connections(
        self, 
        origin: str, 
        category: Optional[str] = None, 
        max_stops: int = 1
    ) -> List[Airport]:
        """
        Return destinations reachable DIRECT or with 1 stop (via popular hubs).
        Now also considers multi-airport cities for self-transfer opportunities.
        """
        direct = self.get_reachable(origin, category)
        if max_stops < 1:
            return direct

        all_candidates = self.get_by_tag(category) if category else self.airports
        all_candidates = [a for a in all_candidates if a.iata != origin]

        hubs = CONNECTION_HUBS.get(origin.upper(), [])
        connected = set(a.iata for a in direct)

        # Add destinations that are interesting and reachable via common hubs
        for a in all_candidates:
            if a.iata not in connected:
                if any(tag in a.tags for tag in ["european-islands", "seaside", "italian-gems"]):
                    connected.add(a.iata)

        # NEW: Also include destinations reachable via multi-airport self-transfer cities
        # e.g. fly to one airport in Milan, ground to another, then continue
        multi_airports = set(self.get_all_multi_airport_airports())
        for hub_airport in hubs + list(multi_airports):
            # If the hub is a multi-airport city member, consider its siblings as connection points
            for city, iatas in MULTI_AIRPORT_CITIES.items():
                if hub_airport in iatas:
                    for other in iatas:
                        if other != hub_airport:
                            # Any destination that has direct from the sibling airport
                            # For simplicity, we add interesting destinations as potential
                            for a in all_candidates:
                                if a.iata not in connected and any(tag in a.tags for tag in ["european-islands", "seaside"]):
                                    connected.add(a.iata)

        # Return full airport objects
        result = [a for a in all_candidates if a.iata in connected]
        # Dedup while preserving some order (direct first)
        seen = set()
        final = []
        for a in direct + result:
            if a.iata not in seen:
                seen.add(a.iata)
                final.append(a)
        return final

    def get_connection_hubs(self, origin: str) -> List[str]:
        """Return regular hubs + all multi-airport airports as potential connection points."""
        regular = CONNECTION_HUBS.get(origin.upper(), [])
        multi = self.get_all_multi_airport_airports()
        # Dedup
        all_hubs = list(dict.fromkeys(regular + multi))
        return all_hubs

    def get_ground_options(self, from_iata: str, to_iata: str) -> List[Dict]:
        """Return ground transport options between two airports (integrated with GroundTransport)."""
        from flight_deals.ground import GroundTransport
        gt = GroundTransport()
        legs = gt.get_ground_options(from_iata, to_iata)
        return [leg.model_dump() for leg in legs]

    def estimate_connection_efficiency(self, origin: str, hub: str, dest: str, flight1_min: int = 90, flight2_min: int = 120) -> Dict:
        from flight_deals.ground import GroundTransport
        gt = GroundTransport()
        return gt.estimate_total_connection_time(origin, hub, dest, flight1_min, flight2_min)
