"""
Ground Transport Module for realistic connection planning (Option A).

Enhancements:
- Smart filtering: only ground legs for reasonable distances (<400km).
- Precomputed data support from ground_transfers.json.
- Efficiency scoring.
- Better total time calc using air duration when available.
"""

import json
import logging
import math
from typing import List, Optional, Dict, Any, Tuple

import requests
from flight_deals.models import GroundLeg
from flight_deals.config import get_config
from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance in km between two points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


class GroundTransport:
    DEFAULT_MAX_GROUND_KM = 400.0

    def __init__(self, use_cache: bool = True, precompute_path: str = "data/ground_transfers.json"):
        self.config = get_config()
        self.use_cache = use_cache
        self.osrm_base = "http://router.project-osrm.org"
        self.transitous_base = "https://api.transitous.org/api/v2"
        self._simple_cache: Dict[str, Any] = {}
        self.precompute_path = resolve_path(precompute_path)
        self._precomputed: Dict[str, List[Dict]] = self._load_precomputed()

    def _load_precomputed(self) -> Dict[str, List[Dict]]:
        if self.precompute_path.exists():
            try:
                data = json.loads(self.precompute_path.read_text())
                return data
            except Exception as e:
                logger.warning("ground: failed to parse precompute file %s: %s", self.precompute_path, e)
        return {}

    def _get_airport_coords(self, iata: str) -> Optional[Tuple[float, float]]:
        from flight_deals.registry.destinations import DestinationRegistry
        reg = DestinationRegistry()
        for a in reg.airports:
            if a.iata.upper() == iata.upper():
                return (a.lat, a.lon)
        return None

    def _cache_key(self, *parts: Any) -> str:
        return "|".join(str(p) for p in parts)

    def is_reasonable_ground_distance(self, from_iata: str, to_iata: str, max_km: float = DEFAULT_MAX_GROUND_KM) -> bool:
        coords_from = self._get_airport_coords(from_iata)
        coords_to = self._get_airport_coords(to_iata)
        if not coords_from or not coords_to:
            return False
        dist = haversine_distance(*coords_from, *coords_to)
        return dist <= max_km

    def get_driving_time(self, from_iata: str, to_iata: str, max_km: float = DEFAULT_MAX_GROUND_KM) -> Optional[GroundLeg]:
        if not self.is_reasonable_ground_distance(from_iata, to_iata, max_km):
            return None

        key = self._cache_key("drive", from_iata, to_iata)
        if key in self._simple_cache:
            return self._simple_cache[key]

        # Check precomputed first
        pre_key = f"{from_iata.upper()}-{to_iata.upper()}"
        if pre_key in self._precomputed:
            for item in self._precomputed[pre_key]:
                if item.get("mode") == "driving":
                    leg = GroundLeg(**item)
                    self._simple_cache[key] = leg
                    return leg

        coords_from = self._get_airport_coords(from_iata)
        coords_to = self._get_airport_coords(to_iata)
        if not coords_from or not coords_to:
            return None

        try:
            url = f"{self.osrm_base}/route/v1/driving/{coords_from[1]},{coords_from[0]};{coords_to[1]},{coords_to[0]}"
            params = {"overview": "false", "annotations": "false"}
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != "Ok" or not data.get("routes"):
                dist = haversine_distance(*coords_from, *coords_to)
                est_min = max(20, int((dist / 80) * 60))
                leg = GroundLeg(
                    from_iata=from_iata,
                    to_iata=to_iata,
                    mode="driving",
                    duration_minutes=est_min,
                    distance_km=round(dist, 1),
                    cost_eur=round(dist * 0.15, 1),
                    notes="Estimated via haversine (OSRM fallback)"
                )
                self._simple_cache[key] = leg
                return leg

            route = data["routes"][0]
            duration_min = int(route["duration"] / 60)
            distance_km = round(route["distance"] / 1000, 1)

            leg = GroundLeg(
                from_iata=from_iata,
                to_iata=to_iata,
                mode="driving",
                duration_minutes=duration_min,
                distance_km=distance_km,
                cost_eur=round(distance_km * 0.12, 1),
                notes="Via OSRM (OpenStreetMap)"
            )
            self._simple_cache[key] = leg
            return leg

        except Exception as e:
            logger.warning("ground: OSRM driving lookup failed for %s->%s, using haversine estimate: %s", from_iata, to_iata, e)
            dist = haversine_distance(*coords_from, *coords_to)
            est_min = max(20, int((dist / 70) * 60))
            leg = GroundLeg(
                from_iata=from_iata,
                to_iata=to_iata,
                mode="driving",
                duration_minutes=est_min,
                distance_km=round(dist, 1),
                notes="Fallback (OSRM error)"
            )
            self._simple_cache[key] = leg
            return leg

    def get_public_transit_options(self, from_iata: str, to_iata: str, date: Optional[str] = None,
                                   max_km: float = DEFAULT_MAX_GROUND_KM) -> List[GroundLeg]:
        if not self.is_reasonable_ground_distance(from_iata, to_iata, max_km):
            return []

        key = self._cache_key("transit", from_iata, to_iata, date or "any")
        if key in self._simple_cache:
            return self._simple_cache[key]

        # Precomputed
        pre_key = f"{from_iata.upper()}-{to_iata.upper()}"
        if pre_key in self._precomputed:
            legs = [GroundLeg(**item) for item in self._precomputed[pre_key] if item.get("mode") != "driving"]
            if legs:
                self._simple_cache[key] = legs
                return legs

        coords_from = self._get_airport_coords(from_iata)
        coords_to = self._get_airport_coords(to_iata)
        if not coords_from or not coords_to:
            return []

        legs: List[GroundLeg] = []
        try:
            url = f"{self.transitous_base}/plan"
            params = {
                "fromPlace": f"{coords_from[0]},{coords_from[1]}",
                "toPlace": f"{coords_to[0]},{coords_to[1]}",
                "transitModes": "TRANSIT",
            }
            if date:
                params["date"] = date

            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for itin in data.get("itineraries", [])[:2]:
                    duration_min = int(itin.get("duration", 0) / 60)
                    if duration_min < 5:
                        continue
                    dist = haversine_distance(*coords_from, *coords_to)
                    mode = "public_transit"
                    notes = "Via Transitous (MOTIS)"
                    if itin.get("legs"):
                        modes = {leg.get("mode", "").lower() for leg in itin["legs"] if "mode" in leg}
                        if modes:
                            mode = ",".join(sorted(modes))
                            notes = f"Transitous: {', '.join(modes)}"

                    legs.append(GroundLeg(
                        from_iata=from_iata,
                        to_iata=to_iata,
                        mode=mode,
                        duration_minutes=duration_min,
                        distance_km=round(dist, 1),
                        cost_eur=15.0,
                        notes=notes
                    ))

            if not legs:
                dist = haversine_distance(*coords_from, *coords_to)
                est = max(15, int((dist / 50) * 60))
                legs.append(GroundLeg(
                    from_iata=from_iata,
                    to_iata=to_iata,
                    mode="public_transit",
                    duration_minutes=est,
                    distance_km=round(dist, 1),
                    notes="Estimated public transit (no live data)"
                ))

            self._simple_cache[key] = legs

        except Exception as e:
            logger.warning("ground: transit lookup failed for %s->%s, using haversine estimate: %s", from_iata, to_iata, e)
            dist = haversine_distance(*coords_from, *coords_to)
            legs.append(GroundLeg(
                from_iata=from_iata,
                to_iata=to_iata,
                mode="public_transit",
                duration_minutes=max(15, int((dist / 45) * 60)),
                distance_km=round(dist, 1),
                notes="Fallback estimate (public transit)"
            ))
            self._simple_cache[key] = legs

        return legs

    def get_ground_options(self, from_iata: str, to_iata: str, prefer: str = "any",
                           max_km: float = DEFAULT_MAX_GROUND_KM) -> List[GroundLeg]:
        options: List[GroundLeg] = []

        if prefer in ("any", "driving"):
            drive = self.get_driving_time(from_iata, to_iata, max_km)
            if drive:
                options.append(drive)

        if prefer in ("any", "public", "train", "bus"):
            transit = self.get_public_transit_options(from_iata, to_iata, max_km=max_km)
            options.extend(transit)

        # Dedup + sort
        seen = set()
        unique = []
        for o in sorted(options, key=lambda x: x.duration_minutes):
            key = (o.mode, o.duration_minutes)
            if key not in seen:
                seen.add(key)
                unique.append(o)

        return unique[:3]

    def estimate_total_connection_time(
        self,
        origin: str,
        hub: str,
        dest: str,
        air_duration_minutes: Optional[int] = None,
        buffer_min: int = 90,
        max_ground_km: float = DEFAULT_MAX_GROUND_KM
    ) -> Dict[str, Any]:
        """Estimate total door-to-door. Uses provided air time or estimates."""
        # Ground only if reasonable
        ground_to_hub = self.get_driving_time(origin, hub, max_ground_km) or \
            GroundLeg(from_iata=origin, to_iata=hub, mode="driving", duration_minutes=0, distance_km=0, notes="N/A (too far)")

        ground_from_hub = self.get_driving_time(hub, dest, max_ground_km) or \
            GroundLeg(from_iata=hub, to_iata=dest, mode="driving", duration_minutes=0, distance_km=0, notes="N/A (too far)")

        air1 = air_duration_minutes or 90
        air2 = air_duration_minutes or 120

        total = air1 + ground_to_hub.duration_minutes + buffer_min + air2 + ground_from_hub.duration_minutes

        return {
            "total_minutes": total,
            "total_hours": round(total / 60, 1),
            "breakdown": {
                "air1": air1,
                "ground_to_hub": ground_to_hub.duration_minutes,
                "buffer": buffer_min,
                "air2": air2,
                "ground_from_hub": ground_from_hub.duration_minutes
            },
            "ground_options": [g for g in [ground_to_hub, ground_from_hub] if g.duration_minutes > 0]
        }

    @staticmethod
    def compute_efficiency_score(price: float, total_minutes: int) -> float:
        """Lower is better: price per hour of total door-to-door time."""
        if total_minutes <= 0:
            return price * 100
        hours = total_minutes / 60.0
        return round(price / hours, 2)


def precompute_ground_transfers(output_path: str = "data/ground_transfers.json", pairs: Optional[List[Tuple[str, str]]] = None):
    """Helper to generate precomputed ground data for common pairs."""
    if pairs is None:
        pairs = [
            ("BUD", "VIE"), ("BUD", "MUC"), ("BUD", "FRA"), ("BUD", "AMS"), ("BUD", "CDG"),
            ("VIE", "MUC"), ("MUC", "FRA"), ("FRA", "AMS"), ("AMS", "CDG"),
            ("BUD", "PRG"), ("VIE", "PRG")
        ]
    gt = GroundTransport(use_cache=False)
    data: Dict[str, List[Dict]] = {}
    for o, d in pairs:
        legs = gt.get_ground_options(o, d, prefer="any")
        if legs:
            data[f"{o}-{d}"] = [leg.model_dump() for leg in legs]
    resolve_path(output_path).write_text(json.dumps(data, indent=2))
    return data
