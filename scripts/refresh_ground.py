#!/usr/bin/env python3
"""Manual / cron refresh of the computed ground matrix — ``data/ground_matrix.json``.

Replaces "6 curated open-jaw pairs" with "any registry pair within a sane ground
crossing", precomputed once so the request path stays clean: the planner and the
registry only ever READ the committed matrix; OSRM is NEVER called from a search
(Task 11 / SEARCH-DESIGN §3). OSRM here is out-of-band, exactly like
``scripts/refresh_fx.py`` hits frankfurter.app — it uses ``requests`` directly
with a proper User-Agent and is outside the ``http.py`` provider rule because it
never runs in the request path.

Model (defined once in ``flight_deals.registry.ground_matrix``; echoed into the
written file):

    ground_minutes = round(drive_minutes * 1.35 + 30)   # transit factor + access pad
    est_cost_eur   = max(8, round(km_road * 0.11))       # ~0.11 EUR/km, 8 EUR floor

These are STATED ESTIMATES, not fake precision — the deal envelope marks them
with ``~`` and ``estimate_basis:"computed"``.

Pipeline:
  1. Load the registry airports (real lat/lon).
  2. Haversine-prefilter pairs: straight-line <= 400 km, excluding same airport
     and same ``multi_city`` group (same city, not an open jaw).
  3. ONE OSRM public ``/table`` request (router.project-osrm.org, driving
     profile, ``annotations=duration,distance``) for the FULL coordinate set —
     they fit under the public 100-location limit (verified: the registry has
     well under 100 airports; the script re-checks and refuses otherwise).
  4. Derive per prefiltered pair: drive_minutes, km_road, ground_minutes,
     est_cost_eur; keep pairs with land ground_minutes <= 330.
  5. Second pass (Task 12): one OSRM ``/route`` (steps=true) per kept pair,
     paced ~1 req/s. Steps with ``mode=="ferry"`` mean a sea crossing — those
     pairs are re-modeled with the tiered ferry estimate (has_ferry/ferry_minutes/
     land_minutes/sea_km, mode "ferry+ground", 420-min cap). A per-pair /route
     failure degrades to ``has_ferry: null`` + warning — UNLESS the pair is
     island-suspect (spans different island regions), in which case it is
     DROPPED rather than kept mispriced as land. An island-region cross-check
     also warns when a land-DETECTED (has_ferry==False) pair looks like it
     should cross water.
  6. Third pass (Task 13, ``--transit`` only): where the free Transitous/MOTIS
     API has scheduled-transit coverage, refine a kept pair's modeled duration
     with a REAL itinerary length (best of two representative departures). Stores
     additive ``transit_minutes``/``transit_transfers``/``transit_modes``/
     ``transit_queried_at``; a scheduled value over the land/ferry cap drops the
     pair (honest "too far"); no coverage keeps the modeled value. AIRPLANE legs
     are excluded (the recipe queries GROUND modes only). Manual-script-only, out
     of the request path exactly like the OSRM passes.
  6b. Fourth pass (Task 14, ``--transit`` only, after the pure pass): for each
     pair the pure pass left at ``no_coverage``, re-query CITY-CENTER anchor ->
     CITY-CENTER anchor for the intercity line-haul and add modeled
     airport-access pads (transit_hybrid_minutes = pad_a + line-haul + pad_b).
     Stores additive ``transit_hybrid_*`` + ``linehaul_minutes``; an HONEST
     HYBRID basis (``scheduled-hybrid``) since the access pads are modeled.
     ~72 extra requests (~36 no_coverage pairs x 2 slots) ~2min.
  7. Atomic write (tmp + os.replace) with schema_version, computed_at, and the
     model params echoed. On a /table failure: clean non-zero exit, the existing
     matrix is left UNTOUCHED (never half-written, never faked). A Transitous
     whole-service failure never invalidates the matrix (table+route stay valid);
     it warns and exits nonzero for the transit pass only.

Usage:

    .venv/bin/python scripts/refresh_ground.py
    .venv/bin/python scripts/refresh_ground.py --dry-run
    .venv/bin/python scripts/refresh_ground.py --transit    # + scheduled refinement
    .venv/bin/python scripts/refresh_ground.py \
        --capture-fixture tests/fixtures/osrm_table_registry.json

Cron example (monthly, 1st at 05:00):

    0 5 1 * *  cd /path/to/flight-deals-tracker && .venv/bin/python scripts/refresh_ground.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from flight_deals.paths import resolve_path
from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry

logger = logging.getLogger("refresh_ground")

OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"
# Second pass (Task 12): one /route per kept pair, steps=true, to detect ferry
# legs (steps carry mode=="ferry"). Manual-script-only, like the /table pass.
OSRM_ROUTE_URL = (
    "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
)
# Polite pacing for the route pass — the public demo server asks for ~1 req/s;
# ~39 kept pairs => ~40s. Global Constraint 9 (the ~1 req/s house rule).
ROUTE_PASS_PACE_SECONDS = 1.0
OSRM_PUBLIC_LOCATION_LIMIT = 100
# A realistic, honest UA identifying the tool (public.project-osrm.org asks for
# a contactable UA on heavy use; this is a once-a-month single call).
USER_AGENT = (
    "flight-deals-tracker/0.7 (open-jaw ground-matrix refresh; "
    "https://github.com/local/flight-deals-tracker)"
)
# Truncate the recorded fixture to a coherent NxN sub-matrix so the committed
# fixture stays lean (mirrors capture_fixtures.py's truncation discipline).
FIXTURE_MAX_NODES = 20

# --- Transit pass (Task 13): Transitous/MOTIS scheduled-transit refinement ---- #
# Third, manual-only pass (after /table + /route). Where the free Transitous API
# (MOTIS) has scheduled-transit coverage for an airport->airport hop, we refine
# the OSRM-modeled duration with a REAL itinerary length. Live-probed 2026-07-12
# (recipe + coverage recorded in .orchestrate/task-13-report.md): the working
# call is GET /api/v1/plan?fromPlace=lat,lon&toPlace=lat,lon&time=...&
# numItineraries=N&transitModes=<ground modes>. Manual-script-only, never in the
# request path (Global Constraints — Transitous joins OSRM in that category).
TRANSITOUS_PLAN_URL = "https://api.transitous.org/api/v1/plan"
# Ground modes ONLY — AIRPLANE is deliberately EXCLUDED. The live probe showed
# that airport-coordinate queries otherwise return absurd air itineraries (e.g.
# a BUD->CAG->VIE two-flight "connection") that are NOT the open-jaw GROUND hop
# we model; excluding air is the load-bearing recipe insight (task-13 report).
TRANSIT_GROUND_MODES = [
    "RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL",
    "REGIONAL_FAST_RAIL", "REGIONAL_RAIL", "BUS", "COACH", "TRAM",
    "SUBWAY", "METRO", "FERRY",
]
# Polite pacing for the transit pass — ~1 req/s house rule; ~38 pairs x 2 slots
# => ~90 requests => ~2 min. Global Constraint 9.
TRANSIT_PASS_PACE_SECONDS = 1.1
# Two representative departures: next Tuesday at least this many days out, at
# 10:00 and 15:00 UTC. UTC is a documented CHOICE — central-Europe locals are
# UTC+1/+2 and this is representative sampling, not per-airport timezone
# precision (explicitly out of scope). Best itinerary = min duration across both.
TRANSIT_MIN_LEAD_DAYS = 14
TRANSIT_SLOTS_UTC = ("10:00:00", "15:00:00")

# --- City-anchor hybrid pass (Task 14) -------------------------------------- #
# After the PURE airport-anchor transit pass, most pairs are still no_coverage
# (airports rarely have an on-site rail/bus stop in Transitous's feeds). The
# HYBRID pass re-queries CITY-CENTER anchor -> CITY-CENTER anchor (the intercity
# line-haul, which the feeds DO cover) and adds modeled airport-access pads on
# each end for comparability with the OSRM airport-to-airport baseline:
#     transit_hybrid_minutes = pad_a + best_city_linehaul_minutes + pad_b
# Same recipe as the pure pass (same two slots, AIRPLANE excluded, ~1s pacing);
# stores additive transit_hybrid_* fields. The read-path acceptance rule
# (registry.ground_matrix.apply_transit_refinement) decides the effective value.


def access_pad_for(airport: Any) -> int:
    """The airport-access pad (minutes) for one end of a hybrid hop: the curated
    per-airport ``access_pad_minutes`` override when set, else the model default
    (``gm.ACCESS_PAD_MINUTES`` = 30)."""
    pad = getattr(airport, "access_pad_minutes", None)
    if isinstance(pad, int) and not isinstance(pad, bool) and pad > 0:
        return pad
    return gm.ACCESS_PAD_MINUTES


class OsrmError(RuntimeError):
    """OSRM was unreachable, refused, or returned an unusable body."""


class TransitousError(RuntimeError):
    """Transitous/MOTIS was unreachable, refused, or returned an unusable body."""


def build_coords(airports: List[Any]) -> str:
    """OSRM wants ``lon,lat`` pairs joined by ``;`` (note: lon FIRST)."""
    return ";".join(f"{a.lon},{a.lat}" for a in airports)


def fetch_table(coords: str, timeout: int = 60) -> Dict[str, Any]:
    """One OSRM ``/table`` request for the full coordinate set. Raises
    ``OsrmError`` on any transport/status/schema problem so the caller can exit
    non-zero WITHOUT touching the existing matrix."""
    url = OSRM_TABLE_URL.format(coords=coords)
    params = {"annotations": "duration,distance"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise OsrmError(f"OSRM request failed: {e}") from e
    if resp.status_code >= 400:
        raise OsrmError(f"OSRM returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise OsrmError(f"OSRM returned non-JSON body: {resp.text[:300]}") from e
    if data.get("code") != "Ok":
        raise OsrmError(f"OSRM code != Ok: {data.get('code')} {data.get('message', '')}")
    if not isinstance(data.get("durations"), list) or not isinstance(data.get("distances"), list):
        raise OsrmError("OSRM response missing durations/distances matrices")
    return data


def fetch_route(lon1: float, lat1: float, lon2: float, lat2: float,
                timeout: int = 30) -> Dict[str, Any]:
    """One OSRM ``/route`` request (steps=true, overview=false) for a single
    A->B pair. Raises ``OsrmError`` on any transport/status/schema problem so the
    caller can degrade THIS pair to ``has_ferry: null`` (never fabricate)."""
    url = OSRM_ROUTE_URL.format(lon1=lon1, lat1=lat1, lon2=lon2, lat2=lat2)
    params = {"steps": "true", "overview": "false"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise OsrmError(f"OSRM /route request failed: {e}") from e
    if resp.status_code >= 400:
        raise OsrmError(f"OSRM /route HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise OsrmError(f"OSRM /route non-JSON body: {resp.text[:200]}") from e
    if data.get("code") != "Ok" or not data.get("routes"):
        raise OsrmError(f"OSRM /route code != Ok: {data.get('code')} {data.get('message', '')}")
    return data


def _route_steps(route_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten every step of the first route (mode/duration/distance carriers)."""
    routes = route_json.get("routes") or []
    if not routes:
        return []
    steps: List[Dict[str, Any]] = []
    for leg in routes[0].get("legs") or []:
        steps.extend(leg.get("steps") or [])
    return steps


def write_route_fixture(out_path: str, a: str, b: str, data: Dict[str, Any],
                        lon1: float, lat1: float, lon2: float, lat2: float,
                        kind: str) -> None:
    """Record one live /route response as a fixture (Task 12 req 6): keep the
    step ``mode``/``duration``/``distance``/``name`` but strip the verbose step
    ``geometry``/``intersections`` (capture_fixtures.py truncation discipline)."""
    routes_in = data.get("routes") or []
    slim_routes: List[Dict[str, Any]] = []
    for r in routes_in[:1]:
        slim_legs = []
        for leg in r.get("legs", []):
            slim_steps = [
                {"mode": s.get("mode"), "duration": s.get("duration"),
                 "distance": s.get("distance"), "name": s.get("name")}
                for s in leg.get("steps", [])
            ]
            slim_legs.append({"duration": leg.get("duration"),
                              "distance": leg.get("distance"), "steps": slim_steps})
        slim_routes.append({"duration": r.get("duration"),
                            "distance": r.get("distance"), "legs": slim_legs})
    fixture = {
        "_captured_live": True,
        "_pair": f"{a}-{b}",
        "_kind": kind,
        "_url": OSRM_ROUTE_URL.format(lon1=lon1, lat1=lat1, lon2=lon2, lat2=lat2),
        "_params": "steps=true&overview=false",
        "_geometry_stripped": True,
        "body": {"code": data.get("code"), "routes": slim_routes},
    }
    path = resolve_path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    logger.info("wrote OSRM /route fixture (%s %s-%s) -> %s", kind, a, b, path)


def run_route_pass(
    rows: List[Dict[str, Any]], airports: List[Any],
    capture_ferry: Optional[str] = None, capture_land: Optional[str] = None,
    pace: float = ROUTE_PASS_PACE_SECONDS,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Second OSRM pass: one /route per land-derived ``row`` to detect ferry
    legs, then re-model ferry pairs (gm.apply_route_pass). Failures degrade to
    ``has_ferry: null`` UNLESS the pair is island-suspect (spans different
    island regions per gm.ferry_detection_suspect) — such a pair is DROPPED
    (never a mispriced land estimate on a route we couldn't verify) and logged.
    If either airport record is missing from the registry, the island-suspect
    check can't even run (no tags to read) — that pair is unverifiable and is
    also DROPPED and logged, rather than kept as an unverifiable
    ``has_ferry: null``. Also runs the island-region detection cross-check on
    has_ferry==False results and records one ferry + one land fixture. Returns
    ``(out_rows, stats)``."""
    by_iata = {a.iata.upper(): a for a in airports}
    tags_by_iata = {a.iata.upper(): set(getattr(a, "tags", []) or []) for a in airports}
    out_rows: List[Dict[str, Any]] = []
    stats = {"ferry": 0, "land": 0, "failed": 0, "dropped_ferry_cap": 0,
             "dropped_island_null": 0, "dropped_unverifiable": 0}
    ferry_captured = land_captured = False
    logger.info("route pass: %d kept pairs, one OSRM /route each (~%.0f req/s)...",
                len(rows), 1.0 / pace if pace else 0)
    for idx, row in enumerate(rows):
        a, b = str(row["a"]).upper(), str(row["b"]).upper()
        apa, apb = by_iata.get(a), by_iata.get(b)
        if apa is None or apb is None:  # defensive: row IATA not in registry
            # No airport record means the island-suspect check can't even run
            # (there are no tags to read) -> unverifiable. Controller ruling:
            # drop the pair rather than keep it as an unverifiable has_ferry:
            # null (it can resurface on the next successful refresh).
            stats["dropped_unverifiable"] += 1
            logger.warning(
                "route pass: airport record missing for pair %s-%s; "
                "excluded rather than kept unverifiable", a, b)
            continue
        if idx:
            time.sleep(pace)
        try:
            data = fetch_route(apa.lon, apa.lat, apb.lon, apb.lat)
        except OsrmError as e:
            if gm.ferry_detection_suspect(tags_by_iata.get(a, set()), tags_by_iata.get(b, set())):
                # A land estimate for an island-crossing pair we couldn't verify
                # would silently mask a possible ferry hop — drop it rather than
                # mispricing (Task 12 quality-review fix), and let the pair
                # resurface on the next successful refresh.
                stats["dropped_island_null"] += 1
                logger.warning(
                    "route pass: route-pass failed for island-crossing pair %s-%s; "
                    "excluded rather than mispriced — re-run refresh (%s)", a, b, e)
                continue
            logger.warning("route pass: %s-%s /route failed (%s) -> has_ferry=null", a, b, e)
            out_rows.append(gm.apply_route_pass(row, None, route_ok=False))
            stats["failed"] += 1
            continue
        steps = _route_steps(data)
        augmented = gm.apply_route_pass(row, steps, route_ok=True)
        if augmented is None:
            stats["dropped_ferry_cap"] += 1
            logger.info("route pass: %s-%s DROPPED (ferry estimate > %dmin cap)",
                        a, b, gm.MAX_FERRY_GROUND_MINUTES)
            continue
        if augmented.get("has_ferry"):
            stats["ferry"] += 1
            logger.info("route pass: %s-%s FERRY (~%smin sea / ~%skm) -> %dmin ~EUR%d %s",
                        a, b, augmented.get("ferry_minutes"), augmented.get("sea_km"),
                        augmented["ground_minutes"], augmented["est_cost_eur"], augmented["mode"])
            if capture_ferry and not ferry_captured:
                write_route_fixture(capture_ferry, a, b, data,
                                    apa.lon, apa.lat, apb.lon, apb.lat, "ferry")
                ferry_captured = True
        else:
            stats["land"] += 1
            if gm.ferry_detection_suspect(tags_by_iata.get(a, set()), tags_by_iata.get(b, set())):
                logger.warning(
                    "route pass: %s-%s spans different island regions but "
                    "has_ferry==False (detection sanity — a sea gap seen as land?)", a, b)
            if capture_land and not land_captured:
                write_route_fixture(capture_land, a, b, data,
                                    apa.lon, apa.lat, apb.lon, apb.lat, "land")
                land_captured = True
        out_rows.append(augmented)
    out_rows.sort(key=lambda r: (r["a"], r["b"]))
    return out_rows, stats


# --------------------------------------------------------------------------- #
# Transit pass (Task 13) — Transitous/MOTIS scheduled-transit refinement        #
# --------------------------------------------------------------------------- #
def next_tuesday_slots(now: Optional[datetime] = None) -> List[str]:
    """The two representative departure ISO instants: the next Tuesday at least
    ``TRANSIT_MIN_LEAD_DAYS`` out, at 10:00 and 15:00 UTC. Deterministic given
    ``now`` (tests freeze it)."""
    now = now or datetime.now(timezone.utc)
    d = now.date() + timedelta(days=TRANSIT_MIN_LEAD_DAYS)
    while d.weekday() != 1:  # Monday=0, Tuesday=1
        d += timedelta(days=1)
    return [f"{d.isoformat()}T{s}Z" for s in TRANSIT_SLOTS_UTC]


def fetch_plan(from_coord: str, to_coord: str, when: str, timeout: int = 40) -> Dict[str, Any]:
    """One Transitous ``/plan`` request for an airport->airport ground itinerary
    at ``when``. Raises ``TransitousError`` on any transport/status/JSON problem
    so the caller can treat THIS pair as no_coverage (never fabricate)."""
    params = {
        "fromPlace": from_coord, "toPlace": to_coord, "time": when,
        "numItineraries": 3, "transitModes": ",".join(TRANSIT_GROUND_MODES),
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        resp = requests.get(TRANSITOUS_PLAN_URL, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise TransitousError(f"Transitous request failed: {e}") from e
    if resp.status_code >= 400:
        raise TransitousError(f"Transitous HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise TransitousError(f"Transitous non-JSON body: {resp.text[:200]}") from e


def _itin_duration_seconds(itin: Dict[str, Any]) -> Optional[int]:
    """The itinerary length in seconds = ``endTime - startTime`` (the brief's
    best-itinerary metric). Uses the ``duration`` field when present (MOTIS sets
    it to exactly that), else computes it from the ISO timestamps."""
    dur = itin.get("duration")
    if isinstance(dur, (int, float)) and not isinstance(dur, bool):
        return int(dur)
    st, en = itin.get("startTime"), itin.get("endTime")
    if isinstance(st, str) and isinstance(en, str):
        try:
            t0 = datetime.fromisoformat(st.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(en.replace("Z", "+00:00"))
            return int((t1 - t0).total_seconds())
        except ValueError:
            return None
    return None


def best_ground_itinerary(
    responses: List[Dict[str, Any]],
) -> Optional[Tuple[int, Optional[int], List[str]]]:
    """Across all ``responses`` (both departure slots), pick the ground itinerary
    with the minimum ``endTime - startTime``. An itinerary is GROUND only if it
    contains NO ``AIRPLANE`` leg (air is excluded — see recipe) and at least one
    real transit leg (a non-WALK mode; a pure-walk 'direct' is not a scheduled
    service). Returns ``(duration_seconds, transfers, transit_modes)`` or ``None``
    when no ground itinerary exists (=> no_coverage)."""
    best: Optional[Tuple[int, Optional[int], List[str]]] = None
    for resp in responses:
        for itin in resp.get("itineraries") or []:
            legs = itin.get("legs") or []
            modes = [l.get("mode") for l in legs]
            if any(m == "AIRPLANE" for m in modes):
                continue
            transit_modes = sorted({m for m in modes if m and m != "WALK"})
            if not transit_modes:
                continue
            dur = _itin_duration_seconds(itin)
            if dur is None:
                continue
            if best is None or dur < best[0]:
                best = (dur, itin.get("transfers"), transit_modes)
    return best


def write_transit_fixture(
    out_path: str, pair: str, kind: str, when: str, resp: Dict[str, Any],
    from_coord: str, to_coord: str,
) -> None:
    """Record one live Transitous ``/plan`` response as a fixture (Task 13 req
    1/6): keep each itinerary's ``duration``/``startTime``/``endTime``/
    ``transfers`` and its legs' ``mode``/``duration``/``from.name``/``to.name``,
    but STRIP the verbose ``legGeometry``/``steps`` (capture truncation
    discipline). Mirrors the probe-captured fixture shape."""
    slim_itins: List[Dict[str, Any]] = []
    for itin in resp.get("itineraries") or []:
        slim_itins.append({
            "duration": itin.get("duration"),
            "startTime": itin.get("startTime"),
            "endTime": itin.get("endTime"),
            "transfers": itin.get("transfers"),
            "legs": [
                {"mode": l.get("mode"), "duration": l.get("duration"),
                 "from": {"name": (l.get("from") or {}).get("name")},
                 "to": {"name": (l.get("to") or {}).get("name")}}
                for l in itin.get("legs") or []
            ],
        })
    fixture = {
        "_captured_live": True,
        "_pair": pair,
        "_kind": kind,
        "_slot": when,
        "_url": TRANSITOUS_PLAN_URL,
        "_params": (f"fromPlace={from_coord}&toPlace={to_coord}&time={when}"
                    f"&numItineraries=3&transitModes={','.join(TRANSIT_GROUND_MODES)}"),
        "_geometry_stripped": True,
        "_note": "legGeometry+steps stripped; itineraries kept",
        "body": {"direct": resp.get("direct", []), "itineraries": slim_itins},
    }
    path = resolve_path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    logger.info("wrote Transitous /plan fixture (%s %s) -> %s", kind, pair, path)


def run_transit_pass(
    rows: List[Dict[str, Any]], airports: List[Any],
    slots: Optional[List[str]] = None, pace: float = TRANSIT_PASS_PACE_SECONDS,
    now: Optional[datetime] = None,
    capture_rail: Optional[str] = None, capture_none: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Third OSRM-analogue pass (Task 13): for each KEPT computed ``row`` query
    Transitous a->b at two departure slots and refine with the best (shortest)
    real scheduled ground itinerary. Stores ADDITIVE per-pair fields
    ``transit_minutes``/``transit_transfers``/``transit_modes``/
    ``transit_queried_at`` (modeled ``ground_minutes``/``est_cost_eur`` stay put —
    the read-path acceptance rule decides the effective value). Rules:

    * no ground itinerary / per-pair request failure -> ``transit: "no_coverage"``,
      modeled values untouched (never fabricate).
    * scheduled minutes over the land/ferry cap (330/420) -> pair DROPPED at
      refresh time with a logged reason (a real timetable saying "too far" is an
      honest exclusion), the pair resurfaces if a later refresh finds it in-cap.
    * whole-service failure (NO slot for ANY pair returned an HTTP body) is
      signalled via ``stats['http_ok'] == 0`` so the caller can warn + exit
      nonzero WITHOUT annotating the matrix (table+route results stay valid).

    Returns ``(out_rows, stats)``. Fixtures: the first refined (rail) pair and
    the first no_coverage pair are recorded when capture paths are given."""
    by_iata = {a.iata.upper(): a for a in airports}
    slots = slots or next_tuesday_slots(now)
    queried_at = datetime.now(timezone.utc).isoformat()
    out_rows: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "refined": 0, "suspect": 0, "no_coverage": 0, "errors": 0,
        "dropped_cap": 0, "http_ok": 0, "slots": list(slots),
    }
    rail_captured = none_captured = False
    logger.info("transit pass: %d kept pairs x %d slots (%s), Transitous /plan, ~%.0f req/s...",
                len(rows), len(slots), ", ".join(slots), 1.0 / pace if pace else 0)
    first = True
    for row in rows:
        a, b = str(row["a"]).upper(), str(row["b"]).upper()
        apa, apb = by_iata.get(a), by_iata.get(b)
        out = dict(row)
        if apa is None or apb is None:  # defensive: no coords -> cannot query
            out["transit"] = "no_coverage"
            stats["no_coverage"] += 1
            out_rows.append(out)
            continue
        from_coord = f"{apa.lat},{apa.lon}"
        to_coord = f"{apb.lat},{apb.lon}"
        responses: List[Dict[str, Any]] = []
        any_ok = False
        for when in slots:
            if not first:
                time.sleep(pace)
            first = False
            try:
                resp = fetch_plan(from_coord, to_coord, when)
            except TransitousError as e:
                logger.warning("transit pass: %s-%s slot %s failed (%s)", a, b, when, e)
                continue
            any_ok = True
            responses.append(resp)
        if not any_ok:  # per-pair whole failure (all slots errored)
            out["transit"] = "no_coverage"
            stats["errors"] += 1
            out_rows.append(out)
            continue
        stats["http_ok"] += 1
        best = best_ground_itinerary(responses)
        if best is None:  # HTTP ok but no ground itinerary
            out["transit"] = "no_coverage"
            stats["no_coverage"] += 1
            if capture_none and not none_captured:
                write_transit_fixture(capture_none, f"{a}-{b}", "no_coverage",
                                      slots[0], responses[0], from_coord, to_coord)
                none_captured = True
            out_rows.append(out)
            continue
        dur_sec, transfers, transit_modes = best
        tmin = int(round(dur_sec / 60.0))
        cap = gm.ground_cap_for(out)
        if tmin > cap:  # honest exclusion: the real timetable says too far
            stats["dropped_cap"] += 1
            logger.info("transit pass: %s-%s DROPPED (scheduled %dmin > %dmin cap)",
                        a, b, tmin, cap)
            continue
        out["transit_minutes"] = tmin
        out["transit_transfers"] = transfers
        out["transit_modes"] = transit_modes
        out["transit_queried_at"] = queried_at
        modeled = int(out.get("ground_minutes") or 0)
        lo, hi = gm.TRANSIT_SUSPECT_LOW * modeled, gm.TRANSIT_SUSPECT_HIGH * modeled
        if modeled and lo <= tmin <= hi:
            stats["refined"] += 1
            logger.info("transit pass: %s-%s REFINED scheduled %dmin (modeled %dmin) "
                        "tf=%s modes=%s", a, b, tmin, modeled,
                        transfers, "+".join(transit_modes))
            if capture_rail and not rail_captured:
                write_transit_fixture(capture_rail, f"{a}-{b}", "rail",
                                      slots[0], responses[0], from_coord, to_coord)
                rail_captured = True
        else:
            stats["suspect"] += 1
            logger.warning("transit pass: %s-%s transit_suspect scheduled %dmin outside "
                           "[%.0f,%.0f] of modeled %dmin — modeled kept at read time",
                           a, b, tmin, lo, hi, modeled)
        out_rows.append(out)
    out_rows.sort(key=lambda r: (r["a"], r["b"]))
    return out_rows, stats


def run_hybrid_pass(
    rows: List[Dict[str, Any]], airports: List[Any],
    slots: Optional[List[str]] = None, pace: float = TRANSIT_PASS_PACE_SECONDS,
    now: Optional[datetime] = None,
    capture_hybrid: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fourth pass (Task 14): for each row the PURE transit pass left at
    ``transit == "no_coverage"``, query CITY-CENTER anchor -> CITY-CENTER anchor
    (same two slots, same recipe incl. AIRPLANE exclusion, ~1s pacing) for the
    intercity line-haul and add modeled airport-access pads:

        transit_hybrid_minutes = pad_a + best_city_linehaul_minutes + pad_b

    Stores ADDITIVE per-pair fields ``transit_hybrid_minutes``/
    ``transit_hybrid_transfers``/``transit_hybrid_modes``/
    ``transit_hybrid_queried_at`` and the raw ``linehaul_minutes`` (the modeled
    ``ground_minutes``/``est_cost_eur`` and the pure ``transit:"no_coverage"``
    marker stay put — the read-path acceptance rule decides the effective value).
    Rows already refined by the pure pass (``transit_minutes`` present), or
    without city anchors, pass through untouched. Rules mirror the pure pass:

    * no ground line-haul itinerary / per-pair failure → stays ``no_coverage``.
    * hybrid minutes over the land/ferry cap (330/420) → pair DROPPED at refresh
      time with a logged reason (an honest "too far", resurfaces if a later
      refresh finds it in-cap).
    * whole-pass failure (NO slot for ANY candidate returned a body) is signalled
      via ``stats['http_ok'] == 0`` so the caller can warn + exit nonzero WITHOUT
      annotating the matrix (table+route+pure-transit results stay valid).

    Returns ``(out_rows, stats)``. The first refined hybrid pair is recorded when
    ``capture_hybrid`` is given."""
    by_iata = {a.iata.upper(): a for a in airports}
    slots = slots or next_tuesday_slots(now)
    queried_at = datetime.now(timezone.utc).isoformat()
    out_rows: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "refined": 0, "suspect": 0, "no_coverage": 0, "errors": 0,
        "dropped_cap": 0, "no_anchor": 0, "http_ok": 0, "candidates": 0,
        "slots": list(slots),
    }
    hybrid_captured = False
    candidates = [r for r in rows if r.get("transit") == "no_coverage"]
    stats["candidates"] = len(candidates)
    logger.info("hybrid pass: %d no_coverage pairs x %d slots (%s), city-anchor "
                "Transitous /plan, ~%.0f req/s...",
                len(candidates), len(slots), ", ".join(slots), 1.0 / pace if pace else 0)
    first = True
    for row in rows:
        out = dict(row)
        if row.get("transit") != "no_coverage":
            out_rows.append(out)  # pure-refined or suspect — leave as-is
            continue
        a, b = str(row["a"]).upper(), str(row["b"]).upper()
        apa, apb = by_iata.get(a), by_iata.get(b)
        ca = (getattr(apa, "city_lat", None), getattr(apa, "city_lon", None)) if apa else (None, None)
        cb = (getattr(apb, "city_lat", None), getattr(apb, "city_lon", None)) if apb else (None, None)
        if None in ca or None in cb:  # no curated city anchor -> cannot query
            stats["no_anchor"] += 1
            out_rows.append(out)
            continue
        from_coord = f"{ca[0]},{ca[1]}"
        to_coord = f"{cb[0]},{cb[1]}"
        responses: List[Dict[str, Any]] = []
        any_ok = False
        for when in slots:
            if not first:
                time.sleep(pace)
            first = False
            try:
                resp = fetch_plan(from_coord, to_coord, when)
            except TransitousError as e:
                logger.warning("hybrid pass: %s-%s slot %s failed (%s)", a, b, when, e)
                continue
            any_ok = True
            responses.append(resp)
        if not any_ok:  # per-pair whole failure (all slots errored)
            stats["errors"] += 1
            out_rows.append(out)
            continue
        stats["http_ok"] += 1
        best = best_ground_itinerary(responses)
        if best is None:  # HTTP ok but no ground line-haul itinerary
            stats["no_coverage"] += 1
            out_rows.append(out)
            continue
        linehaul_sec, transfers, transit_modes = best
        linehaul_min = int(round(linehaul_sec / 60.0))
        pad_a, pad_b = access_pad_for(apa), access_pad_for(apb)
        hybrid_min = pad_a + linehaul_min + pad_b
        cap = gm.ground_cap_for(out)
        if hybrid_min > cap:  # honest exclusion: the real line-haul says too far
            stats["dropped_cap"] += 1
            logger.info("hybrid pass: %s-%s DROPPED (hybrid %dmin = %d+%d+%d > %dmin cap)",
                        a, b, hybrid_min, pad_a, linehaul_min, pad_b, cap)
            continue
        out["transit_hybrid_minutes"] = hybrid_min
        out["transit_hybrid_transfers"] = transfers
        out["transit_hybrid_modes"] = transit_modes
        out["transit_hybrid_queried_at"] = queried_at
        out["linehaul_minutes"] = linehaul_min
        modeled = int(out.get("ground_minutes") or 0)
        lo, hi = gm.TRANSIT_SUSPECT_LOW * modeled, gm.TRANSIT_SUSPECT_HIGH * modeled
        if modeled and lo <= hybrid_min <= hi:
            stats["refined"] += 1
            logger.info("hybrid pass: %s-%s REFINED hybrid %dmin (pads %d+%d + line-haul "
                        "%dmin; modeled %dmin) tf=%s modes=%s", a, b, hybrid_min,
                        pad_a, pad_b, linehaul_min, modeled, transfers, "+".join(transit_modes))
            if capture_hybrid and not hybrid_captured:
                write_transit_fixture(capture_hybrid, f"{a}-{b}", "hybrid",
                                      slots[0], responses[0], from_coord, to_coord)
                hybrid_captured = True
        else:
            stats["suspect"] += 1
            logger.warning("hybrid pass: %s-%s hybrid_suspect %dmin outside [%.0f,%.0f] "
                           "of modeled %dmin — modeled kept at read time",
                           a, b, hybrid_min, lo, hi, modeled)
        out_rows.append(out)
    out_rows.sort(key=lambda r: (r["a"], r["b"]))
    return out_rows, stats


def build_matrix_payload(
    airports: List[Any], prefiltered: List[Tuple[int, int, float]],
    pairs: List[Dict[str, Any]], route_stats: Optional[Dict[str, int]] = None,
    transit_stats: Optional[Dict[str, Any]] = None,
    hybrid_stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "airports": len(airports),
        "prefiltered_candidates": len(prefiltered),
        "pairs_kept": len(pairs),
    }
    if route_stats is not None:
        stats.update({
            "ferry_pairs": route_stats["ferry"],
            "land_pairs": route_stats["land"],
            "route_pass_failed": route_stats["failed"],
            "dropped_ferry_cap": route_stats["dropped_ferry_cap"],
            "dropped_island_null": route_stats.get("dropped_island_null", 0),
            "dropped_unverifiable": route_stats.get("dropped_unverifiable", 0),
        })
    if transit_stats is not None:
        stats.update({
            "transit_refined": transit_stats.get("refined", 0),
            "transit_suspect": transit_stats.get("suspect", 0),
            "transit_no_coverage": transit_stats.get("no_coverage", 0),
            "transit_errors": transit_stats.get("errors", 0),
            "transit_dropped_cap": transit_stats.get("dropped_cap", 0),
            "transit_slots": transit_stats.get("slots", []),
        })
    if hybrid_stats is not None:
        stats.update({
            "hybrid_candidates": hybrid_stats.get("candidates", 0),
            "hybrid_refined": hybrid_stats.get("refined", 0),
            "hybrid_suspect": hybrid_stats.get("suspect", 0),
            "hybrid_no_coverage": hybrid_stats.get("no_coverage", 0),
            "hybrid_errors": hybrid_stats.get("errors", 0),
            "hybrid_dropped_cap": hybrid_stats.get("dropped_cap", 0),
            "hybrid_no_anchor": hybrid_stats.get("no_anchor", 0),
        })
    return {
        "schema_version": gm.SCHEMA_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": (
            "OSRM public router.project-osrm.org /table + /route (driving profile)"
        ),
        "model": dict(gm.MODEL_PARAMS),
        "stats": stats,
        # Additive (Task 11 follow-up): the registry airports that were in the
        # prefilter input at capture time, so a later load can warn when the
        # registry has grown airports the matrix has never seen (drift signal
        # in flight_deals.registry.ground_matrix.check_airport_drift).
        "airports_seen": sorted(a.iata.upper() for a in airports),
        "note": (
            "Computed open-jaw ground hops. ground_minutes/est_cost_eur are STATED "
            "ESTIMATES from an OSRM driving route (see model); the deal envelope "
            "marks them '~' and estimate_basis='computed'. Pairs the /route pass "
            "found to cross water carry has_ferry/ferry_minutes/land_minutes/sea_km "
            "and mode 'ferry+ground' (tiered ferry model). Curated pairs in "
            "data/destinations.json win on merge (estimate_basis='curated')."
        ),
        "pairs": pairs,
    }


def write_atomic(payload: Dict[str, Any]) -> None:
    path = resolve_path(gm.GROUND_MATRIX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def write_fixture(
    out_path: str, airports: List[Any], table: Dict[str, Any], coords_url: str,
) -> None:
    """Record the live OSRM ``/table`` response as a test fixture (Task 11 req
    5), truncated to a coherent ``FIXTURE_MAX_NODES`` x N sub-matrix and with
    the verbose per-node ``hint`` strings stripped — mirroring
    capture_fixtures.py's ``_captured_live`` / ``_truncated`` conventions."""
    n = min(len(airports), FIXTURE_MAX_NODES)
    truncated = n < len(airports)

    def sub(matrix: List[List[Any]]) -> List[List[Any]]:
        return [row[:n] for row in matrix[:n]]

    def strip_nodes(nodes: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(nodes, list):
            return nodes
        return [{"location": node.get("location")} for node in nodes[:n]]

    fixture = {
        "_captured_live": True,
        "_url": coords_url,
        "_annotations": "duration,distance",
        "_airports": [a.iata for a in airports[:n]],
        "_hints_stripped": True,
        "_truncated": truncated,
        "_truncated_to_nodes": n if truncated else None,
        "body": {
            "code": table.get("code"),
            "durations": sub(table["durations"]),
            "distances": sub(table["distances"]),
            "sources": strip_nodes(table.get("sources")),
            "destinations": strip_nodes(table.get("destinations")),
        },
    }
    path = resolve_path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    logger.info("wrote OSRM fixture -> %s (%d nodes%s)", path, n, ", truncated" if truncated else "")


def _print_stats(payload: Dict[str, Any]) -> None:
    st = payload["stats"]
    logger.info(
        "ground matrix: %d airports -> %d prefiltered candidates -> %d pairs kept "
        "(land ground_minutes <= %d)",
        st["airports"], st["prefiltered_candidates"], st["pairs_kept"],
        gm.MAX_GROUND_MINUTES,
    )
    if "ferry_pairs" in st:
        logger.info(
            "  route pass: %d ferry pairs, %d land pairs, %d route failures "
            "(has_ferry=null), %d dropped by the %dmin ferry cap, %d dropped "
            "(island-crossing pair, route pass failed), %d dropped "
            "(airport record missing, unverifiable)",
            st["ferry_pairs"], st["land_pairs"], st["route_pass_failed"],
            st["dropped_ferry_cap"], gm.MAX_FERRY_GROUND_MINUTES,
            st.get("dropped_island_null", 0), st.get("dropped_unverifiable", 0),
        )
    if "transit_refined" in st:
        logger.info(
            "  transit pass: %d refined (scheduled), %d suspect (out of "
            "[0.5x,3.0x], modeled kept), %d no_coverage, %d errors, %d dropped by "
            "the scheduled-value cap; slots %s",
            st["transit_refined"], st["transit_suspect"], st["transit_no_coverage"],
            st.get("transit_errors", 0), st.get("transit_dropped_cap", 0),
            st.get("transit_slots", []),
        )
    if "hybrid_refined" in st:
        logger.info(
            "  hybrid pass: %d no_coverage candidates -> %d refined "
            "(scheduled-hybrid), %d suspect (modeled kept), %d still no_coverage, "
            "%d no city anchor, %d errors, %d dropped by cap",
            st.get("hybrid_candidates", 0), st["hybrid_refined"], st["hybrid_suspect"],
            st.get("hybrid_no_coverage", 0), st.get("hybrid_no_anchor", 0),
            st.get("hybrid_errors", 0), st.get("hybrid_dropped_cap", 0),
        )
    pairs = payload["pairs"]
    if pairs:
        shortest = min(pairs, key=lambda p: p["ground_minutes"])
        longest = max(pairs, key=lambda p: p["ground_minutes"])
        logger.info(
            "  shortest: %s-%s %dmin ~EUR%d | longest kept: %s-%s %dmin ~EUR%d",
            shortest["a"], shortest["b"], shortest["ground_minutes"], shortest["est_cost_eur"],
            longest["a"], longest["b"], longest["ground_minutes"], longest["est_cost_eur"],
        )


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(
        description="Refresh data/ground_matrix.json from OSRM public /table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + derive + print stats, but do NOT write the matrix.")
    ap.add_argument("--capture-fixture", metavar="PATH", default=None,
                    help="Also record the live OSRM /table response to this fixture path.")
    ap.add_argument("--capture-ferry-route", metavar="PATH", default=None,
                    help="Record the first FERRY /route response to this fixture path.")
    ap.add_argument("--capture-land-route", metavar="PATH", default=None,
                    help="Record the first LAND /route response to this fixture path.")
    ap.add_argument("--transit", action="store_true",
                    help="Third pass: refine kept pairs with real Transitous/MOTIS "
                         "scheduled itineraries where coverage exists (manual only, "
                         "~2min; after the table+route passes).")
    ap.add_argument("--capture-transit-rail", metavar="PATH", default=None,
                    help="Record the first REFINED (rail) Transitous /plan response here.")
    ap.add_argument("--capture-transit-none", metavar="PATH", default=None,
                    help="Record the first NO-COVERAGE Transitous /plan response here.")
    ap.add_argument("--capture-hybrid", metavar="PATH", default=None,
                    help="Record the first REFINED city-anchor HYBRID /plan response here.")
    ap.add_argument("--registry", default=None, help="Override destinations.json path.")
    args = ap.parse_args(argv)

    registry = DestinationRegistry(args.registry)
    airports = registry.airports
    if not airports:
        logger.error("refresh_ground: registry has no airports; nothing to do")
        return 1
    if len(airports) > OSRM_PUBLIC_LOCATION_LIMIT:
        logger.error(
            "refresh_ground: %d airports exceeds the OSRM public /table limit of %d; "
            "batching is not implemented (see SEARCH-DESIGN §3 follow-up)",
            len(airports), OSRM_PUBLIC_LOCATION_LIMIT,
        )
        return 1

    groups = gm.group_of(registry.multi_city)
    prefiltered = gm.prefilter_pairs(airports, groups)
    logger.info("prefiltered %d candidate pairs from %d airports; requesting OSRM /table...",
                len(prefiltered), len(airports))

    coords = build_coords(airports)
    coords_url = OSRM_TABLE_URL.format(coords=coords)
    try:
        table = fetch_table(coords)
    except OsrmError as e:
        # Existing matrix is left untouched; clean non-zero exit (Task 11 req 1).
        logger.error("refresh_ground: %s", e)
        logger.error("refresh_ground: existing data/ground_matrix.json left UNCHANGED")
        return 1

    if args.capture_fixture:
        write_fixture(args.capture_fixture, airports, table, coords_url)

    # Table pass -> land-derived rows, then the /route pass detects + re-models
    # ferry crossings (Task 12). The route pass is out-of-band and manual-only,
    # exactly like the /table pass; a per-pair /route failure degrades to
    # has_ferry:null (never a fabricated land pair).
    rows = gm.derive_pairs(airports, table["durations"], table["distances"], prefiltered)
    route_rows, route_stats = run_route_pass(
        rows, airports,
        capture_ferry=args.capture_ferry_route, capture_land=args.capture_land_route,
    )

    # Third pass (Task 13): Transitous/MOTIS scheduled-transit refinement. Runs
    # AFTER table+route and is manual-only (--transit). A whole-service failure
    # (no slot for any pair returned a body) never invalidates the matrix — we
    # discard the (empty) transit annotations, write the table+route matrix, and
    # exit nonzero so a human re-runs the transit pass.
    transit_stats = None
    hybrid_stats = None
    transit_service_failed = False
    if args.transit:
        annotated_rows, transit_stats = run_transit_pass(
            route_rows, airports,
            capture_rail=args.capture_transit_rail,
            capture_none=args.capture_transit_none,
        )
        if transit_stats["http_ok"] == 0 and route_rows:
            transit_service_failed = True
            logger.warning(
                "transit pass: WHOLE-SERVICE FAILURE — no Transitous response for "
                "any pair; matrix written with table+route results only (unrefined)")
            transit_stats = None  # do not record misleading transit stats
        else:
            route_rows = annotated_rows
            # Fourth pass (Task 14): city-anchor HYBRID refinement for the pairs
            # the pure pass left at no_coverage. Whole-pass failure isolation as
            # before — if no candidate returned a body, discard the (empty)
            # hybrid annotations, keep the table+route+pure-transit matrix, and
            # exit nonzero so a human re-runs the hybrid pass.
            hybrid_rows, hybrid_stats = run_hybrid_pass(
                route_rows, airports, capture_hybrid=args.capture_hybrid,
            )
            if hybrid_stats["http_ok"] == 0 and hybrid_stats["candidates"] > 0:
                transit_service_failed = True
                logger.warning(
                    "hybrid pass: WHOLE-PASS FAILURE — no Transitous response for "
                    "any city-anchor candidate; matrix written with "
                    "table+route+pure-transit results only (unrefined)")
                hybrid_stats = None
            else:
                route_rows = hybrid_rows

    payload = build_matrix_payload(airports, prefiltered, route_rows, route_stats,
                                   transit_stats=transit_stats,
                                   hybrid_stats=hybrid_stats)
    _print_stats(payload)

    if args.dry_run:
        logger.info("--dry-run: not writing %s", gm.GROUND_MATRIX_FILE)
        return 1 if transit_service_failed else 0

    write_atomic(payload)
    logger.info("refresh_ground: wrote %d pairs to %s",
                len(payload["pairs"]), resolve_path(gm.GROUND_MATRIX_FILE))
    return 1 if transit_service_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
