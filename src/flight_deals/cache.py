"""
Simple file-based caching for flight search results.
Uses JSON with TTL. Supports stats and advanced invalidation.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from flight_deals.models import FlightDeal
from flight_deals.config import get_config

logger = logging.getLogger(__name__)


# Endpoint -> cache "kind" (which per-endpoint TTL applies). Endpoints are the
# short logical names the provider passes to ResponseCache, not raw URLs.
_KIND_FOR_ENDPOINT = {
    "roundTripFares": "search",
    "cheapestPerDay": "calendar",
    "routes": "routes",
    "timetable": "search",  # Wizz day-level minima — treat like a search result
}


class ResponseCache:
    """
    Cache v2 (Task 3 req 4): caches raw provider response *bodies* keyed by
    ``provider + endpoint + all query params`` (return window and duration
    included), with **per-endpoint TTLs** and **atomic writes**.

    Storing the raw body (not parsed models) keeps the cache provider-agnostic
    and means a schema fix never invalidates on-disk shape — the provider
    parses cached and live bodies through the exact same code path.
    """

    #: default TTLs in seconds, overridden from config in __init__
    DEFAULT_TTLS = {
        "search": 30 * 60,
        "calendar": 6 * 3600,
        "routes": 7 * 86400,
    }

    def __init__(self, cache_dir: Optional[Path] = None, ttls: Optional[Dict[str, float]] = None):
        cfg = get_config()
        self.cache_dir = cache_dir or (cfg.cache_dir / "v2")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if ttls is not None:
            self.ttls = dict(ttls)
        else:
            self.ttls = {
                "search": cfg.cache_ttl_search_minutes * 60,
                "calendar": cfg.cache_ttl_calendar_hours * 3600,
                "routes": cfg.cache_ttl_routes_days * 86400,
            }

    @staticmethod
    def _kind(endpoint: str) -> str:
        return _KIND_FOR_ENDPOINT.get(endpoint, "search")

    def _key(self, provider: str, endpoint: str, params: Dict[str, Any]) -> str:
        # Canonical, order-independent hash of the params (sorted keys).
        blob = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(f"{provider}|{endpoint}|{blob}".encode("utf-8")).hexdigest()[:16]
        return f"{provider}_{endpoint}_{digest}.json"

    def _path(self, provider: str, endpoint: str, params: Dict[str, Any]) -> Path:
        return self.cache_dir / self._key(provider, endpoint, params)

    def get(self, provider: str, endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
        """Return the cached response body, or None on miss / expiry / corruption."""
        path = self._path(provider, endpoint, params)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            cached_at = datetime.fromisoformat(data["cached_at"])
            ttl = timedelta(seconds=self.ttls.get(self._kind(endpoint), 0))
            if datetime.now(timezone.utc) - cached_at > ttl:
                path.unlink(missing_ok=True)
                return None
            return data["body"]
        except Exception as e:
            logger.warning("cache v2: corrupt entry %s, treating as miss: %s", path.name, e)
            return None

    def set(self, provider: str, endpoint: str, params: Dict[str, Any], body: Any) -> None:
        """Atomically write the response body to the cache (tmp + os.replace)."""
        path = self._path(provider, endpoint, params)
        payload = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "endpoint": endpoint,
            "params": params,
            "body": body,
        }
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("cache v2: failed to write %s: %s", path.name, e)
            tmp.unlink(missing_ok=True)

    def clear(self) -> int:
        count = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink(missing_ok=True)
            count += 1
        return count

    def prune_expired(self) -> int:
        """Delete cache entries past their per-endpoint TTL (brief's prune pass,
        UPGRADE-PLAN §4). Returns the number removed. A corrupt/unreadable entry
        is also removed (it can never be a useful hit)."""
        removed = 0
        now = datetime.now(timezone.utc)
        for f in self.cache_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                cached_at = datetime.fromisoformat(data["cached_at"])
                ttl = timedelta(seconds=self.ttls.get(self._kind(data.get("endpoint", "")), 0))
                if now - cached_at > ttl:
                    f.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                f.unlink(missing_ok=True)
                removed += 1
        return removed


class FlightCache:
    def __init__(self, cache_dir: Optional[Path] = None, ttl_hours: Optional[float] = None):
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
        except Exception as e:
            logger.warning("cache: corrupt entry %s, treating as miss: %s", path.name, e)
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
        except Exception as e:
            logger.warning("cache: failed to write %s: %s", path.name, e)

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
                except Exception as e:
                    logger.warning("cache: corrupt entry %s during invalidate, removing: %s", f.name, e)
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
            except Exception as e:
                logger.warning("cache: skipping corrupt entry %s in stats: %s", f.name, e)
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
            except Exception as e:
                logger.warning("cache: corrupt entry %s: %s", f.name, e)
                entries.append({"file": f.name, "error": "corrupt"})
        return entries