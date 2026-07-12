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

Ferry-aware modeling (Task 12): a sea crossing is NOT a road. After the land
model above, ``scripts/refresh_ground.py`` runs a second OSRM ``/route`` pass
(steps=true, manual-script-only) per kept pair; steps with ``mode=="ferry"``
mean the hop crosses water. Such pairs are RE-modeled with a tiered ferry
estimate (see ``FERRY_TIERS`` / :func:`ferry_ground_minutes_for` /
:func:`ferry_est_cost_eur_for`): real ferry fares dwarf EUR 0.11/km and sparse
sailings mean the WAIT dominates, so time = land×1.35 + ferry×1.15 + port +
sailing-wait and cost = land road proxy + a ferry base + per-sea-km, tiered by
``sea_km`` as a sailing-frequency proxy. Ferry hops carry ``mode="ferry+ground"``
and a looser 420-min cap; a failed /route pass degrades to ``has_ferry: null``
(never a fabricated land pair). Curated corridors (``open_jaw_pairs``) still win.

Follow-up (documented, out of scope here): Transitous/MOTIS could later refine
these driving-derived estimates with real public-transport timetables and fares.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)

GROUND_MATRIX_FILE = "data/ground_matrix.json"
SCHEMA_VERSION = 1
STALE_AFTER_DAYS = 45

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

# --- ferry model parameters (Task 12; REVISED per 2026-07-12 corridor research) #
# A sea crossing is NOT a road: real ferry fares are far above EUR 0.11/km and
# sparse sailings mean the WAIT dominates, not the crossing time. When the OSRM
# /route pass (scripts/refresh_ground.py, manual-only) sees ``mode=="ferry"``
# steps we re-model the hop with a ferry-specific, TIERED estimate keyed on
# ``sea_km`` as a sailing-frequency proxy (short straits run turn-up-and-go;
# long crossings are 2-3/day so waiting dominates). Each tier is
# (wait_min, base_eur, eur_per_sea_km, port_access_min):
FERRY_MODE = "ferry+ground"
FERRY_TIME_FACTOR = 1.15  # public-transport-vs-drive factor on the land legs' bus
MAX_FERRY_GROUND_MINUTES = 420  # ferry hops keep a looser cap than land (330)
FERRY_STRAIT_MAX_KM = 15.0   # < 15 km  -> shuttle strait (turn-up-and-go)
FERRY_DOMESTIC_MAX_KM = 60.0  # 15..60 km -> domestic island line (a few/day)
# Tier constants CALIBRATED 2026-07-12 against the five curated corridors (Task
# 12 req 2 sanctions tuning): the brief's revised defaults (strait 15/5/.15/20,
# domestic 60/10/.20/45, long 120/35/.15/45) over-shot duration on the
# long-overland corridors (CFU-PVK, CTA-SUF, HER-JTR at the boundary). These
# tuned values put DURATION within +-40% of ALL FIVE curated corridors and COST
# within +-40% of four of them; the ferry FARE components they produce stay
# realistic (Messina foot ~EUR6, Ionian ~EUR8-10, Aegean fast ~EUR50-54, Malta
# ~EUR50). See .orchestrate/task-12-report.md for the calibration table.
FERRY_TIERS: Dict[str, Dict[str, float]] = {
    "strait":   {"wait": 5,   "base": 5,  "rate": 0.15, "port": 10},
    "domestic": {"wait": 30,  "base": 5,  "rate": 0.15, "port": 30},
    "long":     {"wait": 110, "base": 35, "rate": 0.15, "port": 45},
}
# Named island-region tags (+ 'malta'; + a coarse generic 'island' terrain
# fallback) used only for the detection-sanity cross-check: an open-jaw pair
# spanning two different island regions (or island vs mainland) that the /route
# pass reports as has_ferry==False is logged as suspicious (Task 12 req 1).
ISLAND_REGION_TAGS = frozenset(
    {"sicily", "malta", "crete", "cyclades", "sardinia", "canaries", "baleares"}
)

MODEL_PARAMS: Dict[str, Any] = {
    "haversine_prefilter_km": HAVERSINE_PREFILTER_KM,
    "ground_minutes_formula": "round(drive_minutes * 1.35 + 30)",
    "transit_factor": TRANSIT_FACTOR,
    "access_pad_minutes": ACCESS_PAD_MINUTES,
    "est_cost_eur_formula": "max(8, round(km_road * 0.11))",
    "cost_per_km_eur": COST_PER_KM_EUR,
    "cost_floor_eur": COST_FLOOR_EUR,
    "max_ground_minutes": MAX_GROUND_MINUTES,
    # Ferry model (Task 12) — applies to pairs the /route pass flags has_ferry.
    "ferry_time_formula": (
        "round(land_minutes*1.35 + ferry_minutes*1.15 + port_access + wait)"
    ),
    "ferry_cost_formula": "max(8, round(land_km*0.11)) + base + round(sea_km*rate)",
    "ferry_time_factor": FERRY_TIME_FACTOR,
    "ferry_tiers_by_sea_km": {
        "strait (<15km)": FERRY_TIERS["strait"],
        "domestic (15-60km)": FERRY_TIERS["domestic"],
        "long (>=60km)": FERRY_TIERS["long"],
    },
    "max_ferry_ground_minutes": MAX_FERRY_GROUND_MINUTES,
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
# Ferry model (Task 12) — applied when the /route pass detects a ferry step    #
# --------------------------------------------------------------------------- #
def ferry_tier_for(sea_km: float) -> Dict[str, float]:
    """The ferry tier params for a crossing of ``sea_km`` kilometres (sailing
    frequency proxy): strait shuttle / domestic line / long crossing."""
    if sea_km < FERRY_STRAIT_MAX_KM:
        return FERRY_TIERS["strait"]
    if sea_km < FERRY_DOMESTIC_MAX_KM:
        return FERRY_TIERS["domestic"]
    return FERRY_TIERS["long"]


def ferry_ground_minutes_for(land_minutes: float, ferry_minutes: float, sea_km: float) -> int:
    """Total ferry-hop minutes: the land legs run as buses (×1.35), the crossing
    at ×1.15, plus fixed port-access and sailing-wait pads from the tier."""
    t = ferry_tier_for(sea_km)
    return round(
        max(0.0, land_minutes) * TRANSIT_FACTOR
        + max(0.0, ferry_minutes) * FERRY_TIME_FACTOR
        + t["port"] + t["wait"]
    )


def ferry_est_cost_eur_for(land_km: float, sea_km: float) -> int:
    """Total ferry-hop cost: the land legs at the road proxy (EUR 0.11/km, EUR 8
    floor) + a fixed ferry base fare + a per-sea-km ferry rate from the tier."""
    t = ferry_tier_for(sea_km)
    land_cost = max(COST_FLOOR_EUR, round(max(0.0, land_km) * COST_PER_KM_EUR))
    return int(land_cost + t["base"] + round(max(0.0, sea_km) * t["rate"]))


def parse_ferry_from_steps(steps: Optional[List[Dict[str, Any]]]) -> Tuple[float, float]:
    """Sum ``(ferry_minutes, sea_km)`` over OSRM ``/route`` steps whose
    ``mode == "ferry"``. Durations are seconds, distances metres. A route with
    no ferry step returns ``(0.0, 0.0)``."""
    ferry_sec = 0.0
    ferry_m = 0.0
    for step in steps or []:
        if isinstance(step, dict) and step.get("mode") == "ferry":
            ferry_sec += float(step.get("duration") or 0.0)
            ferry_m += float(step.get("distance") or 0.0)
    return ferry_sec / 60.0, ferry_m / 1000.0


def apply_route_pass(
    row: Dict[str, Any], steps: Optional[List[Dict[str, Any]]], *, route_ok: bool = True,
) -> Optional[Dict[str, Any]]:
    """Augment a land-derived matrix ``row`` (from :func:`derive_pairs`) with the
    result of its OSRM ``/route`` pass (Task 12):

    * ``route_ok is False`` — the /route call failed: attach ``has_ferry: None``
      and keep the land estimate (never fabricate a false-negative land pair).
    * no ferry step — ``has_ferry: False``; the land estimate is authoritative.
    * a ferry step — re-model with the tiered ferry estimate: split the pair's
      ``drive_minutes``/``km_road`` into land vs sea using the ferry step totals,
      set ``mode="ferry+ground"`` and the ``has_ferry``/``ferry_minutes``/
      ``land_minutes``/``sea_km`` fields, and DROP the pair (return ``None``) when
      the ferry estimate exceeds :data:`MAX_FERRY_GROUND_MINUTES` (420)."""
    out = dict(row)
    if not route_ok:
        out["has_ferry"] = None
        return out
    ferry_minutes, sea_km = parse_ferry_from_steps(steps)
    if ferry_minutes <= 0 or sea_km <= 0:
        out["has_ferry"] = False
        return out
    drive_minutes = float(out.get("drive_minutes") or 0.0)
    km_road = float(out.get("km_road") or 0.0)
    land_minutes = max(0.0, drive_minutes - ferry_minutes)
    land_km = max(0.0, km_road - sea_km)
    gm_min = ferry_ground_minutes_for(land_minutes, ferry_minutes, sea_km)
    if gm_min > MAX_FERRY_GROUND_MINUTES:
        return None
    out["ground_minutes"] = gm_min
    out["est_cost_eur"] = ferry_est_cost_eur_for(land_km, sea_km)
    out["mode"] = FERRY_MODE
    out["has_ferry"] = True
    out["ferry_minutes"] = round(ferry_minutes, 1)
    out["land_minutes"] = round(land_minutes, 1)
    out["sea_km"] = round(sea_km, 1)
    out["note"] = (
        f"computed via OSRM (drive ~{round(drive_minutes)}min incl. "
        f"~{round(ferry_minutes)}min ferry / ~{round(sea_km)}km sea crossing)"
    )
    return out


def region_signature(tags: Any) -> frozenset:
    """A coarse island-region signature for the detection cross-check: the named
    island-region tags present, else ``{"island"}`` for an unspecified island,
    else the empty set (mainland)."""
    tagset = {str(t).lower() for t in (tags or [])}
    named = ISLAND_REGION_TAGS & tagset
    if named:
        return frozenset(named)
    if "island" in tagset:
        return frozenset({"island"})
    return frozenset()


def ferry_detection_suspect(tags_a: Any, tags_b: Any) -> bool:
    """True when two airports sit in DIFFERENT island regions (or island vs
    mainland) — so a ``has_ferry == False`` /route detection between them is
    suspicious and worth a logged warning (a sea gap seen as land)."""
    return region_signature(tags_a) != region_signature(tags_b)


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
# Warn-once-per-process flags (mirrors fx.py's ``_check_staleness`` pattern —
# tests reset these via ``monkeypatch.setattr(gm, "_warned_...", False)``).
_warned_stale = False
_warned_schema = False
_warned_drift = False


def _check_staleness(computed_at: Optional[str]) -> None:
    """Warn (once per process) when ``computed_at`` is more than
    ``STALE_AFTER_DAYS`` old. Never raises — the matrix still loads either
    way; this is a signal for a human to run ``scripts/refresh_ground.py``."""
    global _warned_stale
    if _warned_stale or not computed_at:
        return
    try:
        as_of = date.fromisoformat(str(computed_at)[:10])
    except ValueError:
        return
    age = (datetime.now(timezone.utc).date() - as_of).days
    if age > STALE_AFTER_DAYS:
        logger.warning(
            "ground_matrix: ground matrix is %d days old (computed_at %s > %dd) "
            "— run scripts/refresh_ground.py",
            age, computed_at, STALE_AFTER_DAYS,
        )
        _warned_stale = True


def _check_schema_version(schema_version: Any) -> None:
    """Forward-tolerant schema check: an unknown ``schema_version`` is logged,
    never fatal — the matrix still loads (Task 11 minor: warn-and-still-load)."""
    global _warned_schema
    if _warned_schema:
        return
    if schema_version != SCHEMA_VERSION:
        logger.warning(
            "ground_matrix: unknown schema_version %r (expected %d) — loading "
            "anyway (forward-tolerant)",
            schema_version, SCHEMA_VERSION,
        )
        _warned_schema = True


def _load_raw(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read + parse the committed matrix file into its raw dict, or ``None``
    when absent/unreadable. Internal: shared by ``load_ground_matrix`` (which
    extracts ``pairs``) and ``check_airport_drift`` (which needs
    ``airports_seen``)."""
    p = resolve_path(path or GROUND_MATRIX_FILE)
    if not p.exists():
        logger.info("ground_matrix: %s absent; open-jaw uses curated pairs only", p)
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError) as e:
        logger.warning("ground_matrix: could not read %s (%s); curated pairs only", p, e)
        return None
    if not isinstance(data, dict):
        logger.warning("ground_matrix: %s is not a JSON object; curated pairs only", p)
        return None
    return data


def load_ground_matrix(path: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Load the committed ``data/ground_matrix.json`` and return its ``pairs``
    list, or ``None`` when the file is absent or unreadable (the registry then
    serves curated-only — Task 11 req 3 tolerance). Never raises.

    Also surfaces two signals (log only, never fail the load):
    ``schema_version`` mismatch (forward-tolerant) and ``computed_at``
    staleness (> ``STALE_AFTER_DAYS`` old)."""
    data = _load_raw(path)
    if data is None:
        return None
    _check_schema_version(data.get("schema_version"))
    _check_staleness(data.get("computed_at"))
    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        logger.warning("ground_matrix: %s has no 'pairs' list; curated pairs only",
                       resolve_path(path or GROUND_MATRIX_FILE))
        return None
    return pairs


def check_airport_drift(registry_iatas: Set[str], path: Optional[str] = None) -> None:
    """Warn (once per process) how many ``registry_iatas`` are missing from
    the matrix's own recorded ``airports_seen`` census — i.e. registry
    airports added since the matrix was last refreshed. ``airports_seen`` is
    an additive matrix field (a list of IATA codes that were in the prefilter
    input at capture time); older matrices without it are silently skipped
    (nothing to compare against). Never raises."""
    global _warned_drift
    if _warned_drift:
        return
    _warned_drift = True
    data = _load_raw(path)
    if data is None:
        return
    seen = data.get("airports_seen")
    if not isinstance(seen, list):
        return
    seen_set = {str(i).upper() for i in seen}
    missing = sorted({str(i).upper() for i in registry_iatas} - seen_set)
    if missing:
        logger.warning(
            "ground_matrix: %d registry airports missing from ground matrix "
            "(added since last refresh?): %s",
            len(missing), missing[:5],
        )


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
