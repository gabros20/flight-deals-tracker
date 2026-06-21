import json
from pathlib import Path
from typing import List
from flight_deals.models import Airport


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