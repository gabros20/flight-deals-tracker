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
     est_cost_eur; keep pairs with ground_minutes <= 330.
  5. Atomic write (tmp + os.replace) with schema_version, computed_at, and the
     model params echoed. On ANY OSRM failure: clean non-zero exit, the existing
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from flight_deals.paths import resolve_path
from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry

logger = logging.getLogger("refresh_ground")

OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving/{coords}"
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


def build_matrix_payload(
    airports: List[Any], prefiltered: List[Tuple[int, int, float]], table: Dict[str, Any],
) -> Dict[str, Any]:
    pairs = gm.derive_pairs(
        airports, table["durations"], table["distances"], prefiltered,
    )
    return {
        "schema_version": gm.SCHEMA_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "source": "OSRM public router.project-osrm.org /table (driving profile)",
        "model": dict(gm.MODEL_PARAMS),
        "stats": {
            "airports": len(airports),
            "prefiltered_candidates": len(prefiltered),
            "pairs_kept": len(pairs),
        },
        # Additive (Task 11 follow-up): the registry airports that were in the
        # prefilter input at capture time, so a later load can warn when the
        # registry has grown airports the matrix has never seen (drift signal
        # in flight_deals.registry.ground_matrix.check_airport_drift).
        "airports_seen": sorted(a.iata.upper() for a in airports),
        "note": (
            "Computed open-jaw ground hops. ground_minutes/est_cost_eur are STATED "
            "ESTIMATES from an OSRM driving route (see model); the deal envelope "
            "marks them '~' and estimate_basis='computed'. Curated pairs in "
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
        "(ground_minutes <= %d)",
        st["airports"], st["prefiltered_candidates"], st["pairs_kept"],
        gm.MAX_GROUND_MINUTES,
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

    payload = build_matrix_payload(airports, prefiltered, table)
    _print_stats(payload)

    if args.capture_fixture:
        write_fixture(args.capture_fixture, airports, table, coords_url)

    if args.dry_run:
        logger.info("--dry-run: not writing %s", gm.GROUND_MATRIX_FILE)
        return 0

    write_atomic(payload)
    logger.info("refresh_ground: wrote %d pairs to %s",
                len(payload["pairs"]), resolve_path(gm.GROUND_MATRIX_FILE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
