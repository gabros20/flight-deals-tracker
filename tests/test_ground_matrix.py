"""Task 11 — computed ground matrix (open-jaw for any nearby registry pair).

Fixtures-only (Global Constraint 10): the model vectors and merge/cap logic are
pure; the OSRM derivation is exercised against a RECORDED live ``/table``
response (``tests/fixtures/osrm_table_registry.json``, captured by
``scripts/refresh_ground.py --capture-fixture``). No test hits OSRM.
"""

import json
from pathlib import Path

import pytest

from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.engine.planner import (
    Planner, compile_plan, _capped_openjaw_pairs, S4_PAIR_CAP,
)
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare

FIXTURES = Path(__file__).parent / "fixtures"
OSRM_FIXTURE = FIXTURES / "osrm_table_registry.json"


class _AP:
    """Minimal duck-type for the model's prefilter (iata/lat/lon)."""
    def __init__(self, iata, lat, lon):
        self.iata, self.lat, self.lon = iata, lat, lon


# --------------------------------------------------------------------------- #
# model vectors — the formulas are STATED estimates, asserted exactly          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("drive_min, expected", [
    (0, 30),        # round(0*1.35 + 30)
    (100, 165),     # round(135 + 30)
    (120, 192),     # round(162 + 30)
    (222.2, 330),   # the outer edge (round(299.97 + 30) = 330)
])
def test_ground_minutes_model(drive_min, expected):
    assert gm.ground_minutes_for(drive_min) == expected


@pytest.mark.parametrize("km, expected", [
    (0, 8),         # floor
    (50, 8),        # round(5.5) = 6 -> floored to 8
    (100, 11),      # round(11.0)
    (300, 33),      # round(33.0)
    (323.2, 36),    # LIS-OPO-ish
])
def test_est_cost_model(km, expected):
    assert gm.est_cost_eur_for(km) == expected


def test_haversine_km_known_distance():
    # BUD (47.4369, 19.2556) -> VIE (48.1103, 16.5697): ~214 km great-circle.
    km = gm.haversine_km(47.4369, 19.2556, 48.1103, 16.5697)
    assert 210 <= km <= 220


# --------------------------------------------------------------------------- #
# haversine prefilter — range + same-airport + same multi_city group exclusion #
# --------------------------------------------------------------------------- #
def test_prefilter_range_and_group_exclusion():
    airports = [
        _AP("MXP", 45.63, 8.72),   # Milan (group Milan)
        _AP("BGY", 45.67, 9.70),   # Milan (group Milan) — same group, ~75km
        _AP("VCE", 45.50, 12.35),  # Venice — ~280km from MXP
        _AP("LIS", 38.77, -9.13),  # Lisbon — >1500km, out of range
    ]
    groups = {"MXP": "Milan", "BGY": "Milan"}
    pairs = gm.prefilter_pairs(airports, groups, max_km=400)
    got = {(airports[i].iata, airports[j].iata) for i, j, _ in pairs}
    assert ("MXP", "VCE") in got          # within 400km, different cities -> kept
    assert ("MXP", "BGY") not in got      # same multi_city group -> excluded
    assert ("BGY", "VCE") in got
    assert all("LIS" not in pair for pair in got)  # out of 400km range


def test_prefilter_excludes_identical_airport():
    airports = [_AP("AAA", 40.0, 10.0), _AP("AAA", 40.0, 10.0)]
    assert gm.prefilter_pairs(airports, {}, max_km=400) == []


# --------------------------------------------------------------------------- #
# derivation against the recorded live OSRM /table response                     #
# --------------------------------------------------------------------------- #
def test_derive_pairs_from_recorded_osrm_fixture():
    fx = json.loads(OSRM_FIXTURE.read_text())
    assert fx["_captured_live"] is True
    body = fx["body"]
    iatas = fx["_airports"]
    # Reconstruct airport duck-types from the recorded source locations
    # ([lon, lat]) so prefilter + derive run exactly as the script does.
    airports = [_AP(iata, node["location"][1], node["location"][0])
                for iata, node in zip(iatas, body["sources"])]
    groups = {}  # the 20-node fixture head has no same-group pairs
    prefiltered = gm.prefilter_pairs(airports, groups)
    rows = gm.derive_pairs(airports, body["durations"], body["distances"], prefiltered)

    assert rows, "expected at least one routable ground pair in the fixture"
    for r in rows:
        # schema + model invariants on every derived row
        assert set(r) >= {"a", "b", "ground_minutes", "est_cost_eur", "mode",
                          "km_road", "drive_minutes", "note"}
        assert r["a"] < r["b"]                       # canonical unordered key
        assert r["ground_minutes"] <= gm.MAX_GROUND_MINUTES
        assert r["est_cost_eur"] >= gm.COST_FLOOR_EUR
        assert r["km_road"] >= r["straight_km"] * gm.ROAD_SANITY_FACTOR
        # the row's ground_minutes must equal the model applied to its drive time
        assert r["ground_minutes"] == gm.ground_minutes_for(r["drive_minutes"])
    # CTA<->PMO (Sicily) are both in the fixture head and road-connected.
    keys = {(r["a"], r["b"]) for r in rows}
    assert ("CTA", "PMO") in keys


def test_derive_drops_disconnected_zero_route():
    # Two "airports" 200km apart but OSRM returns a degenerate ~0 route
    # (disconnected road components — the Canary-islands failure mode). The
    # road-sanity guard must drop it rather than fabricate a 30-min hop.
    airports = [_AP("ISL", 28.0, -13.6), _AP("JSL", 28.5, -15.4)]
    prefiltered = gm.prefilter_pairs(airports, {}, max_km=400)
    assert prefiltered, "the two points are within 400km straight-line"
    durations = [[0.0, 5.0], [5.0, 0.0]]     # ~0 seconds
    distances = [[0.0, 50.0], [50.0, 0.0]]   # ~0 metres road vs ~180km straight
    assert gm.derive_pairs(airports, durations, distances, prefiltered) == []


# --------------------------------------------------------------------------- #
# committed matrix file — schema validation                                     #
# --------------------------------------------------------------------------- #
def test_committed_matrix_schema():
    pairs = gm.load_ground_matrix()
    assert pairs is not None and len(pairs) > 0
    from flight_deals.paths import resolve_path
    data = json.loads(resolve_path(gm.GROUND_MATRIX_FILE).read_text())
    assert data["schema_version"] == 1
    assert "computed_at" in data and "model" in data
    assert data["model"]["transit_factor"] == gm.TRANSIT_FACTOR
    registry_iatas = {a.iata for a in DestinationRegistry().airports}
    for p in pairs:
        assert p["a"] in registry_iatas and p["b"] in registry_iatas
        assert p["a"] != p["b"]
        assert 0 < p["ground_minutes"] <= gm.MAX_GROUND_MINUTES
        assert p["est_cost_eur"] >= gm.COST_FLOOR_EUR


# --------------------------------------------------------------------------- #
# merge — curated wins, computed tagged, absent-matrix fallback                 #
# --------------------------------------------------------------------------- #
def test_merge_curated_wins_over_computed():
    curated = [{"a": "NAP", "b": "BRI", "ground_minutes": 240, "est_cost_eur": 35,
                "mode": "train", "note": "curated"}]
    computed = [
        {"a": "BRI", "b": "NAP", "ground_minutes": 284, "est_cost_eur": 27, "mode": "public_transit"},
        {"a": "AHO", "b": "OLB", "ground_minutes": 181, "est_cost_eur": 14, "mode": "public_transit"},
    ]
    merged = gm.merge_open_jaw_pairs(curated, computed)
    by_key = {frozenset({p["a"], p["b"]}): p for p in merged}
    nap_bri = by_key[frozenset({"NAP", "BRI"})]
    assert nap_bri["estimate_basis"] == "curated"
    assert nap_bri["ground_minutes"] == 240          # curated value preserved
    assert nap_bri["note"] == "curated"              # not overridden by computed
    assert by_key[frozenset({"AHO", "OLB"})]["estimate_basis"] == "computed"
    assert len(merged) == 2                           # BRI/NAP computed dedup'd out


def test_merge_absent_matrix_is_curated_only():
    curated = [{"a": "SPU", "b": "ZAD", "ground_minutes": 180, "est_cost_eur": 15, "mode": "bus"}]
    merged = gm.merge_open_jaw_pairs(curated, None)
    assert len(merged) == 1
    assert merged[0]["estimate_basis"] == "curated"


def test_registry_tolerates_absent_matrix(tmp_path):
    reg = DestinationRegistry(ground_matrix_path=str(tmp_path / "does_not_exist.json"))
    pairs = reg.get_open_jaw_pairs()
    assert pairs and all(p["estimate_basis"] == "curated" for p in pairs)
    # the 6 curated pairs survive with their exact values
    assert {frozenset({p["a"], p["b"]}) for p in pairs} >= {frozenset({"NAP", "BRI"})}


def test_registry_merges_committed_matrix():
    reg = DestinationRegistry()  # default committed matrix
    pairs = reg.get_open_jaw_pairs()
    bases = {p["estimate_basis"] for p in pairs}
    assert bases == {"curated", "computed"}
    curated = [p for p in pairs if p["estimate_basis"] == "curated"]
    assert len(curated) == 6  # the 6 hand-curated pairs, untouched


# --------------------------------------------------------------------------- #
# pair cap — 40 shortest-ground, visible dropped count, no silent truncation    #
# --------------------------------------------------------------------------- #
def test_capped_openjaw_pairs_keeps_shortest_and_reports_dropped():
    matched = {f"A{i:02d}" for i in range(60)} | {f"B{i:02d}" for i in range(60)}

    class FakeReg:
        def get_open_jaw_pairs(self):
            # 60 pairs with ground_minutes 60,61,...,119 (all matched)
            return [{"a": f"A{i:02d}", "b": f"B{i:02d}",
                     "ground_minutes": 60 + i, "est_cost_eur": 10, "mode": "public_transit"}
                    for i in range(60)]

    kept, dropped = _capped_openjaw_pairs(FakeReg(), matched, cap=S4_PAIR_CAP)
    assert len(kept) == S4_PAIR_CAP == 40
    assert dropped == 20
    # the KEPT pairs are the 40 SHORTEST-ground ones (60..99), not an arbitrary slice
    assert max(p["ground_minutes"] for p in kept) == 99
    assert kept[0]["ground_minutes"] == 60


def test_plan_reports_openjaw_drop_count(monkeypatch):
    """A capped run must surface the dropped count in the plan output — no
    silent truncation (Task 11 req 4)."""
    reg = DestinationRegistry()
    matched_iatas = {a.iata for a in reg.matching("italy & seaside")}
    many = [{"a": a, "b": b, "ground_minutes": 90 + k, "est_cost_eur": 12, "mode": "public_transit"}
            for k, (a, b) in enumerate(
                (x, y) for i, x in enumerate(sorted(matched_iatas))
                for y in sorted(matched_iatas)[i + 1:])]
    assert len(many) > S4_PAIR_CAP, "need more than the cap to force a drop"
    monkeypatch.setattr(reg, "get_open_jaw_pairs", lambda: many)

    spec = parse_spec({
        "origins": ["BUD"], "where": "italy & seaside", "depart": "2026-08-22..2026-08-24",
        "nights": "5-8", "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })
    plan = compile_plan(spec, reg).to_dict()
    assert plan["openjaw_pairs_considered"] == S4_PAIR_CAP
    assert plan["openjaw_pairs_dropped"] == len(many) - S4_PAIR_CAP


def test_non_openjaw_plan_omits_openjaw_fields():
    """Additive fields must be ABSENT on plans without the open-jaw shape, so
    existing plan envelopes stay byte-identical."""
    spec = parse_spec({
        "origins": ["BUD"], "where": "italy & seaside", "depart": "2026-08-22..2026-08-24",
        "nights": "5-8", "shapes": ["direct"], "carriers": ["ryanair"],
    })
    plan = compile_plan(spec).to_dict()
    assert "openjaw_pairs_considered" not in plan
    assert "openjaw_pairs_dropped" not in plan


# --------------------------------------------------------------------------- #
# S4 end-to-end: the planner discovers a COMPUTED (non-curated) pair            #
# --------------------------------------------------------------------------- #
def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="cal")


def test_planner_surfaces_a_computed_openjaw_pair():
    """AHO<->OLB (Sardinia) is a COMPUTED pair from the committed matrix, not one
    of the 6 curated ones. With CAL fares present, the planner must surface it as
    an S4 open-jaw deal whose ground carries estimate_basis='computed'."""
    reg = DestinationRegistry()
    computed = {frozenset({p["a"], p["b"]}) for p in reg.get_open_jaw_pairs()
                if p["estimate_basis"] == "computed"}
    assert frozenset({"AHO", "OLB"}) in computed, "AHO-OLB should be a computed matrix pair"

    spec = parse_spec({
        "origins": ["BUD"], "where": "italy & seaside", "depart": "2026-08-22..2026-08-24",
        "nights": "5-8", "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })

    cal = {
        ("BUD", "AHO"): [("2026-08-22", 30.0)],
        ("OLB", "BUD"): [("2026-08-28", 25.0)],
        ("BUD", "OLB"): [("2026-08-22", 90.0)],
        ("AHO", "BUD"): [("2026-08-28", 90.0)],
    }
    p = Planner(registry=reg)
    p.ryanair.roundtrip_fares = lambda origin, dest=None, **k: []
    p.ryanair.cheapest_per_day = lambda origin, dest, month, **k: [
        _df(origin, dest, d, pr) for d, pr in cal.get((origin, dest), [])]
    p.wizz.timetable = lambda *a, **k: ([], [])

    results = p.execute(compile_plan(spec, reg), spec)["results"]
    oj = [d for d in results if d["shape"] == "S4"
          and {d["destination"], d["legs"][-1]["origin"]} == {"AHO", "OLB"}]
    assert oj, "expected the computed AHO-OLB open-jaw to surface"
    d = oj[0]
    assert d["ground"]["estimate_basis"] == "computed"
    assert d["price_confidence"] == "exact"
    assert d["price_eur"] == 30.0 + 25.0 + d["ground"]["cost_eur"]
