"""
Ground Transport Module for realistic connection planning.

Option A integration: Provides distance, travel time, and options between airports.
Sources (free-first):
- Haversine for baseline distance
- OSRM (public or self-hosted) for driving time/distance
- Transitous/MOTIS public API for public transport options (optional)

Heavily cached where possible. Designed for European hub connections.
"""

import math
import requests
from typing import List, Optional, Dict, Any
from flight_deals.models import GroundLeg
from flight_deals.config import get_config


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance in km between two points."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


class GroundTransport:
    def __init__(self, use_cache: bool = True):
        self.config = get_config()
        self.use_cache = use_cache
        self.osrm_base = "http://router.project-osrm.org"
        self.transitous_base = "https://api.transitous.org/api/v2"
        # Simple in-memory cache for this session (avoids type issues with FlightCache)
        self._simple_cache: Dict[str, Any] = {}

    def _get_airport_coords(self, iata: str) -> Optional[tuple]:
        from flight_deals.registry.destinations import DestinationRegistry
        reg = DestinationRegistry()
        for a in reg.airports:
            if a.iata.upper() == iata.upper():
                return (a.lat, a.lon)
        return None

    def _cache_key(self, *parts):
        return "|".join(str(p) for p in parts)

    def get_driving_time(self, from_iata: str, to_iata: str) -> Optional[GroundLeg]:
        key = self._cache_key("drive", from_iata, to_iata)
        if key in self._simple_cache:
            return self._simple_cache[key]

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
                est_min = int((dist / 80) * 60)
                leg = GroundLeg(
                    from_iata=from_iata,
                    to_iata=to_iata,
                    mode="driving",
                    duration_minutes=est_min,
                    distance_km=round(dist, 1),
                    estimated_cost_eur=round(dist * 0.15, 1),
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
                estimated_cost_eur=round(distance_km * 0.12, 1),
                notes="Via OSRM (OpenStreetMap)"
            )
            self._simple_cache[key] = leg
            return leg

        except Exception as e:
            dist = haversine_distance(coords_from[0], coords_from[1], coords_to[0], coords_to[1])
            est_min = max(30, int((dist / 70) * 60))
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

    def get_public_transit_options(self, from_iata: str, to_iata: str, date: Optional[str] = None) -> List[GroundLeg]:
        key = self._cache_key("transit", from_iata, to_iata, date or "any")
        if key in self._simple_cache:
            return self._simple_cache[key]

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
                        modes = set()
                        for leg in itin["legs"]:
                            if "mode" in leg:
                                modes.add(leg["mode"].lower())
                        if modes:
                            mode = ",".join(sorted(modes))
                            notes = f"Transitous: {', '.join(modes)}"

                    legs.append(GroundLeg(
                        from_iata=from_iata,
                        to_iata=to_iata,
                        mode=mode,
                        duration_minutes=duration_min,
                        distance_km=round(dist, 1),
                        estimated_cost_eur=15.0,
                        notes=notes
                    ))

            if not legs:
                dist = haversine_distance(*coords_from, *coords_to)
                est = int((dist / 50) * 60)
                legs.append(GroundLeg(
                    from_iata=from_iata,
                    to_iata=to_iata,
                    mode="public_transit",
                    duration_minutes=est,
                    distance_km=round(dist, 1),
                    notes="Estimated public transit (no live data)"
                ))

            self._simple_cache[key] = legs

        except Exception:
            dist = haversine_distance(*coords_from, *coords_to)
            legs.append(GroundLeg(
                from_iata=from_iata,
                to_iata=to_iata,
                mode="public_transit",
                duration_minutes=int((dist / 45) * 60),
                distance_km=round(dist, 1),
                notes="Fallback estimate (public transit)"
            ))
            self._simple_cache[key] = legs

        return legs

    def get_ground_options(self, from_iata: str, to_iata: str, prefer: str = "any") -> List[GroundLeg]:
        options: List[GroundLeg] = []

        drive = self.get_driving_time(from_iata, to_iata)
        if drive:
            options.append(drive)

        if prefer in ("any", "public", "train", "bus"):
            transit = self.get_public_transit_options(from_iata, to_iata)
            options.extend(transit)

        seen = set()
        unique = []
        for o in sorted(options, key=lambda x: x.duration_minutes):
            key = (o.mode, o.duration_minutes)
            if key not in seen:
                seen.add(key)
                unique.append(o)

        return unique[:3]

    def estimate_total_connection_time(
        self, origin: str, hub: str, dest: str, 
        flight1_min: int, flight2_min: int, buffer_min: int = 90
    ) -> Dict[str, Any]:
        ground_to_hub = self.get_driving_time(origin, hub) or GroundLeg(from_iata=origin, to_iata=hub, mode="driving", duration_minutes=60, distance_km=100)
        ground_from_hub = self.get_driving_time(hub, dest) or GroundLeg(from_iata=hub, to_iata=dest, mode="driving", duration_minutes=60, distance_km=100)

        total = flight1_min + ground_to_hub.duration_minutes + buffer_min + flight2_min + ground_from_hub.duration_minutes

        return {
            "total_minutes": total,
            "total_hours": round(total / 60, 1),
            "breakdown": {
                "flight1": flight1_min,
                "ground_to_hub": ground_to_hub.duration_minutes,
                "buffer": buffer_min,
                "flight2": flight2_min,
                "ground_from_hub": ground_from_hub.duration_minutes
            },
            "ground_options": [ground_to_hub, ground_from_hub]
        }
