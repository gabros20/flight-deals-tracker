"""The computed ground-matrix model + loader (Task 11).

This module is the single home of the ground-crossing estimate model — the
formulas that turn an OSRM driving route (duration + distance) into an honest
public-transport ground estimate for an open-jaw hop between two nearby
airports. Both the offline refresh script (``scripts/refresh_ground.py``, the
WRITE path — it does the OSRM HTTP) and the registry (the READ path — it merges
the committed matrix with the curated pairs) import from here, so the model is
defined exactly once.

Model (stated estimates, NOT fake precision — the deal envelope marks them with
``~`` and ``estimate_basis:"computed"``):

    ground_minutes = round(drive_minutes * 1.35 + 30)
        * 1.35 — public-transport-vs-drive factor (a bus/train intercity leg
          runs slower than a private car on the same corridor).
        * +30  — fixed airport-access pad (getting from the arrival airport to
          the intercity station and from the station to the departure airport).
    est_cost_eur   = max(8, round(km_road * 0.11))
        * 0.11 EUR/km — blended intercity bus/train fare-per-km proxy.
        * 8 EUR floor — the shortest sensible single ticket.

Prefilter: straight-line (haversine) distance <= 400 km, excluding same airport
and same ``multi_city`` group (those aren't open-jaw — they're one city with two
airports). Kept pairs are additionally capped at ``ground_minutes <= 330`` (a
5h30 ground crossing is the outer edge of "worth it for an open jaw").

Follow-up (documented, out of scope here): Transitous/MOTIS could later refine
these driving-derived estimates with real public-transport timetables and fares.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)

GROUND_MATRIX_FILE = "data/ground_matrix.json"

# --- model parameters (echoed into the written matrix + docstring above) ----- #
HAVERSINE_PREFILTER_KM = 400.0
TRANSIT_FACTOR = 1.35
ACCESS_PAD_MINUTES = 30
COST_PER_KM_EUR = 0.11
COST_FLOOR_EUR = 8
MAX_GROUND_MINUTES = 330
GROUND_MODE = "public_transit"
# A routed road distance can never be materially shorter than the straight-line
# (great-circle) distance. When OSRM returns a distance well below it, the two
# airports are in DISCONNECTED road-network components (e.g. separate islands
# with no road/ferry link in the driving graph) and OSRM snapped both to a
# degenerate ~0 route — a fabricated "30min hop across open sea". Such pairs are
# dropped, never estimated (Global Constraint 3: no fabricated data). The 0.85
# tolerance allows for airport-coordinate snapping to the nearest road.
ROAD_SANITY_FACTOR = 0.85

MODEL_PARAMS: Dict[str, Any] = {
    "haversine_prefilter_km": HAVERSINE_PREFILTER_KM,
    "ground_minutes_formula": "round(drive_minutes * 1.35 + 30)",
    "transit_factor": TRANSIT_FACTOR,
    "access_pad_minutes": ACCESS_PAD_MINUTES,
    "est_cost_eur_formula": "max(8, round(km_road * 0.11))",
    "cost_per_km_eur": COST_PER_KM_EUR,
    "cost_floor_eur": COST_FLOOR_EUR,
    "max_ground_minutes": MAX_GROUND_MINUTES,
}


# --------------------------------------------------------------------------- #
# Pure model functions                                                         #
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometres."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ground_minutes_for(drive_minutes: float) -> int:
    """Public-transport ground minutes from an OSRM drive time (the model)."""
    return round(drive_minutes * TRANSIT_FACTOR + ACCESS_PAD_MINUTES)


def est_cost_eur_for(km_road: float) -> int:
    """Estimated ground ticket cost in EUR from the routed road distance."""
    return max(COST_FLOOR_EUR, round(km_road * COST_PER_KM_EUR))


# --------------------------------------------------------------------------- #
# Prefilter + derivation (WRITE path — used by scripts/refresh_ground.py)       #
# --------------------------------------------------------------------------- #
def group_of(multi_city: Dict[str, List[str]]) -> Dict[str, str]:
    """IATA -> multi_city group name (city), for same-city exclusion."""
    out: Dict[str, str] = {}
    for city, iatas in multi_city.items():
        for i in iatas:
            out[i.upper()] = city
    return out


def prefilter_pairs(
    airports: List[Any], groups: Dict[str, str], max_km: float = HAVERSINE_PREFILTER_KM,
) -> List[Tuple[int, int, float]]:
    """Straight-line prefilter: return ``(i, j, straight_km)`` for every airport
    index pair (i<j) within ``max_km`` that is not the same airport and not two
    airports of the same ``multi_city`` group. ``airports`` items must expose
    ``.iata``, ``.lat``, ``.lon`` (a ``models.Airport`` or any duck-type)."""
    out: List[Tuple[int, int, float]] = []
    n = len(airports)
    for i in range(n):
        a = airports[i]
        for j in range(i + 1, n):
            b = airports[j]
            if a.iata.upper() == b.iata.upper():
                continue
            ga, gb = groups.get(a.iata.upper()), groups.get(b.iata.upper())
            if ga is not None and ga == gb:
                continue  # same multi-airport city — not an open jaw
            km = haversine_km(a.lat, a.lon, b.lat, b.lon)
            if km <= max_km:
                out.append((i, j, round(km, 1)))
    return out


def _cell(matrix: List[List[Optional[float]]], i: int, j: int) -> Optional[float]:
    try:
        return matrix[i][j]
    except (IndexError, TypeError):
        return None


def derive_pairs(
    airports: List[Any],
    durations: List[List[Optional[float]]],
    distances: List[List[Optional[float]]],
    prefiltered: List[Tuple[int, int, float]],
) -> List[Dict[str, Any]]:
    """Turn the OSRM ``/table`` duration (seconds) + distance (metres) matrices
    into open-jaw pair rows for the prefiltered index pairs, applying the model
    and the ``ground_minutes <= 330`` cut. Both routing directions are averaged
    (a ground hop can be travelled either way; averaging is robust to one-way
    routing quirks). Unroutable pairs (null cells) are dropped. Rows are keyed
    with ``a < b`` by IATA and returned sorted for a byte-stable matrix file."""
    rows: List[Dict[str, Any]] = []
    for i, j, straight_km in prefiltered:
        dur_ij, dur_ji = _cell(durations, i, j), _cell(durations, j, i)
        dist_ij, dist_ji = _cell(distances, i, j), _cell(distances, j, i)
        durs = [d for d in (dur_ij, dur_ji) if d is not None]
        dists = [d for d in (dist_ij, dist_ji) if d is not None]
        if not durs or not dists:
            continue  # unroutable (e.g. an island with no road/ferry link)
        drive_minutes = (sum(durs) / len(durs)) / 60.0
        km_road = (sum(dists) / len(dists)) / 1000.0
        if km_road < straight_km * ROAD_SANITY_FACTOR:
            continue  # disconnected components (no real road/ferry link) — not a ground hop
        gm = ground_minutes_for(drive_minutes)
        if gm > MAX_GROUND_MINUTES:
            continue
        a_iata, b_iata = airports[i].iata.upper(), airports[j].iata.upper()
        if a_iata > b_iata:
            a_iata, b_iata = b_iata, a_iata
        rows.append({
            "a": a_iata,
            "b": b_iata,
            "ground_minutes": gm,
            "est_cost_eur": est_cost_eur_for(km_road),
            "mode": GROUND_MODE,
            "drive_minutes": round(drive_minutes, 1),
            "km_road": round(km_road, 1),
            "straight_km": straight_km,
            "note": f"computed via OSRM (drive ~{round(drive_minutes)}min / {round(km_road)}km road)",
        })
    rows.sort(key=lambda r: (r["a"], r["b"]))
    return rows


# --------------------------------------------------------------------------- #
# Loader (READ path — used by the registry)                                    #
# --------------------------------------------------------------------------- #
def load_ground_matrix(path: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Load the committed ``data/ground_matrix.json`` and return its ``pairs``
    list, or ``None`` when the file is absent or unreadable (the registry then
    serves curated-only — Task 11 req 3 tolerance). Never raises."""
    p = resolve_path(path or GROUND_MATRIX_FILE)
    if not p.exists():
        logger.info("ground_matrix: %s absent; open-jaw uses curated pairs only", p)
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError) as e:
        logger.warning("ground_matrix: could not read %s (%s); curated pairs only", p, e)
        return None
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        logger.warning("ground_matrix: %s has no 'pairs' list; curated pairs only", p)
        return None
    return pairs


def merge_open_jaw_pairs(
    curated: List[Dict[str, Any]], computed: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Merge curated (authoritative) + computed open-jaw pairs. Curated pairs
    keep their exact values and win — a computed pair for the same unordered
    ``{a, b}`` airport combo is dropped, never overriding curation. Every pair
    is tagged with ``estimate_basis`` (``curated`` | ``computed``)."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for p in curated:
        q = dict(p)
        q["estimate_basis"] = "curated"
        out.append(q)
        seen.add(frozenset({str(p["a"]).upper(), str(p["b"]).upper()}))
    for p in computed or []:
        key = frozenset({str(p["a"]).upper(), str(p["b"]).upper()})
        if key in seen:
            continue
        q = dict(p)
        q["estimate_basis"] = "computed"
        out.append(q)
        seen.add(key)
    return out
