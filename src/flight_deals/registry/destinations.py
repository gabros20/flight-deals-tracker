import json
from pathlib import Path
from typing import List, Optional, Set
from flight_deals.models import Airport


# Basic known direct connections from popular origins (can be expanded)
# Format: origin -> set of known destinations
KNOWN_DIRECT_ROUTES = {
    "BUD": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CHQ", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "LPA", "TFS", "FUE", "BRI", "SUF", "BDS", "BGY", "BCN", "MAD", "VIE"},
    "STN": {"PMI", "IBZ", "CFU", "HER", "CTA", "PMO", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN", "MAD"},
    "BGY": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "CAG", "OLB", "ALC", "AGP", "FAO", "BRI", "SUF", "BDS"},
    "DUB": {"PMI", "IBZ", "CFU", "HER", "ALC", "AGP", "FAO", "LPA", "TFS", "BGY", "BCN"},
    "VIE": {"PMI", "IBZ", "CFU", "HER", "ZTH", "CTA", "PMO", "ALC", "AGP", "FAO", "BRI", "SUF"},
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