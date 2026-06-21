"""
Simple file-based caching for flight search results.
Uses JSON with TTL.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Any, Dict
from flight_deals.models import FlightDeal
from flight_deals.config import get_config


class FlightCache:
    def __init__(self, cache_dir: Optional[Path] = None, ttl_hours: Optional[int] = None):
        config = get_config()
        self.cache_dir = cache_dir or config.cache_dir
        self.ttl = timedelta(hours=ttl_hours or config.cache_ttl_hours)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_key(self, provider: str, origin: str, date_from: str, date_to: str, destination: Optional[str] = None) -> str:
        dest_part = destination or "ANY"
        return f"{provider}_{origin}_{dest_part}_{date_from}_{date_to}.json"

    def _get_path(self, key: str) -> Path:
        return self.cache_dir / key

    def get(self, provider: str, origin: str, date_from: str, date_to: str, destination: Optional[str] = None) -> Optional[List[FlightDeal]]:
        key = self._get_key(provider, origin, date_from, date_to, destination)
        path = self._get_path(key)

        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            cached_time = datetime.fromisoformat(data["timestamp"])
            if datetime.now() - cached_time > self.ttl:
                path.unlink(missing_ok=True)
                return None

            deals = []
            for item in data["deals"]:
                deals.append(FlightDeal(**item))
            return deals
        except Exception:
            return None

    def set(self, provider: str, origin: str, date_from: str, date_to: str, deals: List[FlightDeal], destination: Optional[str] = None):
        key = self._get_key(provider, origin, date_from, date_to, destination)
        path = self._get_path(key)

        payload = {
            "timestamp": datetime.now().isoformat(),
            "provider": provider,
            "origin": origin,
            "destination": destination or "ANY",
            "date_from": date_from,
            "date_to": date_to,
            "deals": [d.model_dump() for d in deals],
        }

        try:
            path.write_text(json.dumps(payload, indent=2))
        except Exception:
            pass  # Fail silently on cache write

    def clear(self):
        for f in self.cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)

    def invalidate(self, provider: str = None, origin: str = None):
        """Remove specific cache entries"""
        for f in self.cache_dir.glob("*.json"):
            if provider and provider not in f.name:
                continue
            if origin and origin not in f.name:
                continue
            f.unlink(missing_ok=True)