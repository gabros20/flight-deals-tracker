"""
Simple file-based caching for flight search results.
Uses JSON with TTL. Supports stats and advanced invalidation.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
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
            pass

    def clear(self):
        """Remove all cached entries"""
        count = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)
            count += 1
        return count

    def invalidate(self, provider: Optional[str] = None, origin: Optional[str] = None, older_than_hours: Optional[int] = None):
        """
        Remove specific cache entries.
        - provider: e.g. "ryanair"
        - origin: e.g. "BUD"
        - older_than_hours: remove entries older than this many hours
        """
        count = 0
        now = datetime.now()
        cutoff = now - timedelta(hours=older_than_hours) if older_than_hours else None

        for f in list(self.cache_dir.glob("*.json")):
            remove = False
            if provider and provider not in f.name:
                continue
            if origin and origin not in f.name:
                continue

            if cutoff:
                try:
                    data = json.loads(f.read_text())
                    ts = datetime.fromisoformat(data.get("timestamp", ""))
                    if ts < cutoff:
                        remove = True
                except Exception:
                    remove = True  # corrupt file

            if remove or (not cutoff and (provider or origin)):
                f.unlink(missing_ok=True)
                count += 1

        return count

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics"""
        files = list(self.cache_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in files)
        
        timestamps = []
        providers = set()
        origins = set()

        for f in files:
            try:
                data = json.loads(f.read_text())
                ts = datetime.fromisoformat(data.get("timestamp", ""))
                timestamps.append(ts)
                providers.add(data.get("provider", "unknown"))
                origins.add(data.get("origin", "unknown"))
            except Exception:
                continue

        oldest = min(timestamps).isoformat() if timestamps else None
        newest = max(timestamps).isoformat() if timestamps else None

        return {
            "total_entries": len(files),
            "total_size_bytes": total_size,
            "total_size_kb": round(total_size / 1024, 1),
            "providers": sorted(providers),
            "origins": sorted(origins),
            "oldest_entry": oldest,
            "newest_entry": newest,
            "ttl_hours": self.ttl.total_seconds() / 3600,
        }

    def list_entries(self) -> List[Dict[str, Any]]:
        """List all cache entries with metadata"""
        entries = []
        for f in sorted(self.cache_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                entries.append({
                    "file": f.name,
                    "provider": data.get("provider"),
                    "origin": data.get("origin"),
                    "destination": data.get("destination"),
                    "date_from": data.get("date_from"),
                    "date_to": data.get("date_to"),
                    "cached_at": data.get("timestamp"),
                    "num_deals": len(data.get("deals", [])),
                })
            except Exception:
                entries.append({"file": f.name, "error": "corrupt"})
        return entries