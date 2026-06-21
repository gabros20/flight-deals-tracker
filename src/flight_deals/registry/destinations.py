import json
from pathlib import Path
from typing import List, Optional, Set, Dict
from flight_deals.models import Airport


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
CONNECTION_HUBS: Dict[str, List[str]] = {
    "BUD": ["VIE", "MUC", "FRA", "AMS", "CDG", "IST", "PRG"]  # Common good connection points
}


class DestinationRegistry:
    def __init__(self, data_path: str = "data/destinations.json"):
        self.data_path = Path(data_path)
        self.airports: List[Airport] = []
        self._load()

    def _load(self):
        if self.data_path.exists():
            data = json.loads(self.data_path.read_text())
            self.airports = [Airport(**item) for item in data]

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

    def get_reachable_with_connections(
        self, 
        origin: str, 
        category: Optional[str] = None, 
        max_stops: int = 1
    ) -> List[Airport]:
        """
        Return destinations reachable DIRECT or with 1 stop (via popular hubs).
        For connections, we include interesting destinations even if no direct LCC flight.
        """
        direct = self.get_reachable(origin, category)
        if max_stops < 1:
            return direct

        all_candidates = self.get_by_tag(category) if category else self.airports
        all_candidates = [a for a in all_candidates if a.iata != origin]

        hubs = CONNECTION_HUBS.get(origin.upper(), [])
        connected = set(a.iata for a in direct)

        # Add destinations that are interesting and reachable via common hubs
        # (user can then search BUD->hub + hub->dest or use other airlines)
        for a in all_candidates:
            if a.iata not in connected:
                # Prioritize places that are popular 1-stop targets or have good tags
                if any(tag in a.tags for tag in ["european-islands", "seaside", "italian-gems"]):
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

    def get_all_tags(self) -> Set[str]:
        tags: Set[str] = set()
        for a in self.airports:
            tags.update(a.tags)
        return tags

    def get_connection_hubs(self, origin: str) -> List[str]:
        return CONNECTION_HUBS.get(origin.upper(), [])