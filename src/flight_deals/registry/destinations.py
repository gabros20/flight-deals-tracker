import json
from pathlib import Path
from typing import List, Optional, Set
from flight_deals.models import Airport


# Known direct connections from popular Ryanair & Wizz bases (easily extendable)
# Format: origin -> set of common direct destinations
# Sources: Common routes from airline route maps (updated 2026)
KNOWN_DIRECT_ROUTES = {
    # Major hubs
    "BUD": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "JTR", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "SUF", "BDS", "BGY", "BCN", "MAD", "VIE", "EDI", "STN"},
    "STN": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "SUF", "BDS", "BGY", "BCN", "MAD", "VIE", "DUB"},
    "BGY": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "JTR", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "SUF", "BDS", "BCN", "MAD"},
    "DUB": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "CTA", "PMO", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "BGY", "BCN", "MAD", "STN", "EDI"},
    "VIE": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "CTA", "PMO", "ALC", "AGP", "FAO", "BRI", "SUF", "BDS", "BGY"},
    
    # UK & Ireland
    "EDI": {"PMI", "IBZ", "CFU", "HER", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN", "MAD", "BUD"},
    "MAN": {"PMI", "IBZ", "CFU", "HER", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN"},
    
    # Spain & Italy bases
    "BCN": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "BRI", "SUF", "BDS", "BUD", "VIE"},
    "MAD": {"PMI", "IBZ", "CFU", "HER", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "BUD"},
    "BRI": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "BGY", "BCN"},
    
    # Canary Islands as origins (seasonal)
    "LPA": {"BUD", "STN", "BGY", "DUB", "EDI", "MAD", "BCN"},
    "TFS": {"BUD", "STN", "BGY", "DUB", "EDI", "MAD", "BCN"},
    
    # Others
    "VLC": {"PMI", "IBZ", "CFU", "HER", "BUD", "BGY"},
    "FAO": {"BUD", "STN", "BGY", "DUB", "EDI"},
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
        # Simple version - in real version would filter reachable routes
        return [a for a in self.airports if a.iata != origin_iata]

    def get_reachable(self, origin: str, category: Optional[str] = None) -> List[Airport]:
        """
        Return destinations that are likely reachable from the given origin.
        Uses known direct routes + falls back to all if unknown.
        """
        candidates = self.get_by_tag(category) if category else self.airports
        candidates = [a for a in candidates if a.iata != origin]

        known = KNOWN_DIRECT_ROUTES.get(origin.upper(), set())
        if known:
            # Prioritize known direct routes
            reachable = [a for a in candidates if a.iata in known]
            if reachable:
                return reachable

        # Fallback: return all candidates (the providers will return empty for unreachable anyway)
        return candidates

    def get_all_tags(self) -> Set[str]:
        tags: Set[str] = set()
        for a in self.airports:
            tags.update(a.tags)
        return tags