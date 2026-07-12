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
  6. Atomic write (tmp + os.replace) with schema_version, computed_at, and the
     model params echoed. On a /table failure: clean non-zero exit, the existing
     matrix is left UNTOUCHED (never half-written, never faked).

Usage:

    .venv/bin/python scripts/refresh_ground.py
    .venv/bin/python scripts/refresh_ground.py --dry-run
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
from datetime import datetime, timezone
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


class OsrmError(RuntimeError):
    """OSRM was unreachable, refused, or returned an unusable body."""


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
    Also runs the island-region detection cross-check on has_ferry==False
    results and records one ferry + one land fixture. Returns ``(out_rows,
    stats)``."""
    by_iata = {a.iata.upper(): a for a in airports}
    tags_by_iata = {a.iata.upper(): set(getattr(a, "tags", []) or []) for a in airports}
    out_rows: List[Dict[str, Any]] = []
    stats = {"ferry": 0, "land": 0, "failed": 0, "dropped_ferry_cap": 0,
             "dropped_island_null": 0}
    ferry_captured = land_captured = False
    logger.info("route pass: %d kept pairs, one OSRM /route each (~%.0f req/s)...",
                len(rows), 1.0 / pace if pace else 0)
    for idx, row in enumerate(rows):
        a, b = str(row["a"]).upper(), str(row["b"]).upper()
        apa, apb = by_iata.get(a), by_iata.get(b)
        if apa is None or apb is None:  # defensive: row IATA not in registry
            out_rows.append(gm.apply_route_pass(row, None, route_ok=False))
            stats["failed"] += 1
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


def build_matrix_payload(
    airports: List[Any], prefiltered: List[Tuple[int, int, float]],
    pairs: List[Dict[str, Any]], route_stats: Optional[Dict[str, int]] = None,
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
            "(island-crossing pair, route pass failed)",
            st["ferry_pairs"], st["land_pairs"], st["route_pass_failed"],
            st["dropped_ferry_cap"], gm.MAX_FERRY_GROUND_MINUTES,
            st.get("dropped_island_null", 0),
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
    payload = build_matrix_payload(airports, prefiltered, route_rows, route_stats)
    _print_stats(payload)

    if args.dry_run:
        logger.info("--dry-run: not writing %s", gm.GROUND_MATRIX_FILE)
        return 0

    write_atomic(payload)
    logger.info("refresh_ground: wrote %d pairs to %s",
                len(payload["pairs"]), resolve_path(gm.GROUND_MATRIX_FILE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
