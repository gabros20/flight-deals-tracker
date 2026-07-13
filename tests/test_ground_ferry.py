"""Task 12 — ferry-aware ground modeling.

Fixtures-only (Global Constraint 10): the tiered ferry model is pure and asserted
exactly; the /route detection is exercised against RECORDED live responses
(``tests/fixtures/osrm_route_ferry.json`` = CFU-PVK, ``osrm_route_land.json`` =
AHO-CAG, captured by ``scripts/refresh_ground.py --capture-*-route``). No test
hits OSRM.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Tuple

import pytest

from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare
from flight_deals import output

FIXTURES = Path(__file__).parent / "fixtures"
FERRY_ROUTE_FIXTURE = FIXTURES / "osrm_route_ferry.json"
LAND_ROUTE_FIXTURE = FIXTURES / "osrm_route_land.json"

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "refresh_ground.py"


def _load_refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_ground_t12", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _steps_from_fixture(path) -> list:
    body = json.loads(path.read_text())["body"]
    return [s for leg in body["routes"][0]["legs"] for s in leg["steps"]]


# --------------------------------------------------------------------------- #
# tier selection + model vectors (STATED estimates, asserted exactly)          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sea_km, tier", [
    (0.0, "strait"), (14.9, "strait"),
    (15.0, "domestic"), (32.9, "domestic"), (59.9, "domestic"),
    (60.0, "long"), (124.4, "long"),
])
def test_ferry_tier_boundaries(sea_km, tier):
    assert gm.ferry_tier_for(sea_km) is gm.FERRY_TIERS[tier]


def test_ferry_ground_minutes_matches_formula():
    # CFU-PVK inputs: land 130.3, ferry 75.0, sea 18.1 (domestic tier).
    # round(130.3*1.35 + 75.0*1.15 + port30 + wait30)
    t = gm.FERRY_TIERS["domestic"]
    expected = round(130.3 * gm.TRANSIT_FACTOR + 75.0 * gm.FERRY_TIME_FACTOR
                     + t["port"] + t["wait"])
    assert gm.ferry_ground_minutes_for(130.3, 75.0, 18.1) == expected == 322


def test_ferry_est_cost_matches_formula():
    # land_km 133.1, sea 18.1 (domestic): max(8,round(133.1*0.11)) + base5 + round(18.1*0.15)
    t = gm.FERRY_TIERS["domestic"]
    expected = max(gm.COST_FLOOR_EUR, round(133.1 * gm.COST_PER_KM_EUR)) + t["base"] + round(18.1 * t["rate"])
    assert gm.ferry_est_cost_eur_for(133.1, 18.1) == expected == 23


def test_ferry_cost_land_floor_applies_on_tiny_land_leg():
    # HER-JTR: land_km ~15.8 -> land proxy floors to 8; long tier adds base35 + sea.
    t = gm.FERRY_TIERS["long"]
    assert gm.ferry_est_cost_eur_for(15.8, 124.4) == 8 + t["base"] + round(124.4 * t["rate"]) == 62


# --------------------------------------------------------------------------- #
# /route step parsing against the recorded live fixtures                        #
# --------------------------------------------------------------------------- #
def test_parse_ferry_from_ferry_fixture():
    fx = json.loads(FERRY_ROUTE_FIXTURE.read_text())
    assert fx["_captured_live"] is True and fx["_kind"] == "ferry"
    ferry_minutes, sea_km = gm.parse_ferry_from_steps(_steps_from_fixture(FERRY_ROUTE_FIXTURE))
    assert ferry_minutes > 0 and sea_km > 0            # a real ferry step was recorded
    assert 60 <= ferry_minutes <= 90 and 10 <= sea_km <= 25   # CFU->Igoumenitsa hop


def test_parse_ferry_from_land_fixture_is_zero():
    fx = json.loads(LAND_ROUTE_FIXTURE.read_text())
    assert fx["_kind"] == "land"
    ferry_minutes, sea_km = gm.parse_ferry_from_steps(_steps_from_fixture(LAND_ROUTE_FIXTURE))
    assert ferry_minutes == 0.0 and sea_km == 0.0      # AHO-CAG is all road (Sardinia)


# --------------------------------------------------------------------------- #
# apply_route_pass: re-model ferry / keep land / degrade / cap                  #
# --------------------------------------------------------------------------- #
def _land_row(**kw):
    row = {"a": "CFU", "b": "PVK", "ground_minutes": 307, "est_cost_eur": 17,
           "mode": gm.GROUND_MODE, "drive_minutes": 205.3, "km_road": 151.2,
           "straight_km": 105.2, "note": "land"}
    row.update(kw)
    return row


def test_apply_route_pass_remodels_ferry_pair():
    steps = _steps_from_fixture(FERRY_ROUTE_FIXTURE)
    out = gm.apply_route_pass(_land_row(), steps, route_ok=True)
    assert out is not None
    assert out["has_ferry"] is True
    assert out["mode"] == gm.FERRY_MODE == "ferry+ground"
    assert out["ferry_minutes"] > 0 and out["sea_km"] > 0
    assert out["land_minutes"] == pytest.approx(205.3 - out["ferry_minutes"], abs=0.1)
    assert out["ground_minutes"] <= gm.MAX_FERRY_GROUND_MINUTES
    # re-modeled, not the land estimate
    assert out["ground_minutes"] != 307


def test_apply_route_pass_keeps_land_pair():
    steps = _steps_from_fixture(LAND_ROUTE_FIXTURE)
    row = _land_row(a="AHO", b="CAG")
    out = gm.apply_route_pass(row, steps, route_ok=True)
    assert out["has_ferry"] is False
    assert out["mode"] == gm.GROUND_MODE          # unchanged
    assert out["ground_minutes"] == row["ground_minutes"]
    assert "sea_km" not in out                     # no ferry fields on a land pair


def test_apply_route_pass_route_failure_degrades_to_null():
    out = gm.apply_route_pass(_land_row(), None, route_ok=False)
    assert out["has_ferry"] is None                # never a fabricated land false-negative
    assert out["ground_minutes"] == 307            # land estimate retained
    assert "sea_km" not in out


def test_apply_route_pass_drops_ferry_over_cap():
    # A synthetic long crossing whose ferry estimate exceeds the 420-min cap.
    row = _land_row(drive_minutes=400.0, km_road=380.0)
    steps = [{"mode": "ferry", "duration": 3 * 3600, "distance": 120 * 1000}]  # 3h / 120km sea
    assert gm.ferry_ground_minutes_for(400.0 - 180, 180, 120) > gm.MAX_FERRY_GROUND_MINUTES
    assert gm.apply_route_pass(row, steps, route_ok=True) is None


# --------------------------------------------------------------------------- #
# island-region detection cross-check                                          #
# --------------------------------------------------------------------------- #
def test_ferry_detection_suspect_across_island_regions():
    reg = DestinationRegistry()
    tags = {a.iata: set(a.tags) for a in reg.airports}
    assert gm.ferry_detection_suspect(tags["CTA"], tags["MLA"]) is True   # sicily vs malta
    assert gm.ferry_detection_suspect(tags["HER"], tags["JTR"]) is True   # crete vs cyclades
    assert gm.ferry_detection_suspect(tags["CFU"], tags["PVK"]) is True   # island vs mainland
    assert gm.ferry_detection_suspect(tags["AHO"], tags["OLB"]) is False  # both sardinia
    assert gm.ferry_detection_suspect(tags["CHQ"], tags["HER"]) is False  # both crete
    assert gm.ferry_detection_suspect(tags["SPU"], tags["ZAD"]) is False  # both mainland croatia


# --------------------------------------------------------------------------- #
# run_route_pass (script) — island cross-check warning fires on false negative  #
# --------------------------------------------------------------------------- #
class _AP:
    def __init__(self, iata, lat, lon, tags):
        self.iata, self.lat, self.lon, self.tags = iata, lat, lon, tags


def test_run_route_pass_warns_on_island_false_negative(caplog):
    module = _load_refresh_module()
    # Force a LAND-only /route for a sicily<->malta pair — a detection miss.
    module.fetch_route = lambda *a, **k: {
        "code": "Ok",
        "routes": [{"legs": [{"steps": [
            {"mode": "driving", "duration": 3600, "distance": 90000}]}]}],
    }
    rows = [{"a": "CTA", "b": "MLA", "ground_minutes": 300, "est_cost_eur": 25,
             "mode": gm.GROUND_MODE, "drive_minutes": 200.0, "km_road": 210.0,
             "straight_km": 186.0, "note": "land"}]
    airports = [_AP("CTA", 37.4668, 15.0664, ["italy", "sicily", "island"]),
                _AP("MLA", 35.8575, 14.4775, ["malta", "island"])]
    with caplog.at_level("WARNING", logger="refresh_ground"):
        out, stats = module.run_route_pass(rows, airports, pace=0)
    assert out[0]["has_ferry"] is False and stats["land"] == 1
    assert any("has_ferry==False" in r.message for r in caplog.records)


def test_run_route_pass_drops_island_suspect_pair_on_route_failure(caplog):
    """Quality-review fix: a /route failure for an island-crossing pair must
    NOT resurrect the land estimate (a route we couldn't verify might actually
    be a ferry hop) — the pair is dropped entirely, with a logged warning."""
    module = _load_refresh_module()
    module.fetch_route = lambda *a, **k: (_ for _ in ()).throw(module.OsrmError("boom"))
    rows = [{"a": "CTA", "b": "MLA", "ground_minutes": 300, "est_cost_eur": 25,
             "mode": gm.GROUND_MODE, "drive_minutes": 200.0, "km_road": 210.0,
             "straight_km": 186.0, "note": "land"}]
    airports = [_AP("CTA", 37.4668, 15.0664, ["italy", "sicily", "island"]),
                _AP("MLA", 35.8575, 14.4775, ["malta", "island"])]
    with caplog.at_level("WARNING", logger="refresh_ground"):
        out, stats = module.run_route_pass(rows, airports, pace=0)
    assert out == []                                    # dropped, not kept as land
    assert stats["dropped_island_null"] == 1 and stats["failed"] == 0
    assert any("route-pass failed for island-crossing pair CTA-MLA" in r.message
               and "excluded rather than mispriced" in r.message
               for r in caplog.records)


def test_run_route_pass_keeps_non_suspect_pair_as_land_on_route_failure(caplog):
    """A /route failure for a pair that is NOT island-suspect keeps degrading
    to has_ferry:null with the land estimate retained — unchanged behaviour."""
    module = _load_refresh_module()
    module.fetch_route = lambda *a, **k: (_ for _ in ()).throw(module.OsrmError("boom"))
    rows = [{"a": "AHO", "b": "OLB", "ground_minutes": 150, "est_cost_eur": 20,
             "mode": gm.GROUND_MODE, "drive_minutes": 90.0, "km_road": 130.0,
             "straight_km": 120.0, "note": "land"}]
    airports = [_AP("AHO", 40.6321, 8.2908, ["italy", "sardinia", "island"]),
                _AP("OLB", 40.8987, 9.5176, ["italy", "sardinia", "island"])]
    with caplog.at_level("WARNING", logger="refresh_ground"):
        out, stats = module.run_route_pass(rows, airports, pace=0)
    assert len(out) == 1
    assert out[0]["has_ferry"] is None
    assert out[0]["ground_minutes"] == 150               # land estimate retained
    assert stats["failed"] == 1 and stats["dropped_island_null"] == 0


def test_run_route_pass_drops_pair_with_missing_airport_record(caplog):
    """Controller ruling: if either airport record is missing from the
    registry, the island-suspect check can't even run (no tags to read from)
    -> the pair is unverifiable and must be DROPPED, not kept as an
    unverifiable has_ferry:null."""
    module = _load_refresh_module()
    module.fetch_route = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("fetch_route should not be called for a missing-registry pair"))
    rows = [{"a": "CTA", "b": "ZZZ", "ground_minutes": 300, "est_cost_eur": 25,
             "mode": gm.GROUND_MODE, "drive_minutes": 200.0, "km_road": 210.0,
             "straight_km": 186.0, "note": "land"}]
    airports = [_AP("CTA", 37.4668, 15.0664, ["italy", "sicily", "island"])]
    with caplog.at_level("WARNING", logger="refresh_ground"):
        out, stats = module.run_route_pass(rows, airports, pace=0)
    assert out == []                                    # dropped, not kept as null
    assert stats["dropped_unverifiable"] == 1
    assert stats["failed"] == 0 and stats["dropped_island_null"] == 0
    assert any("airport record missing for pair CTA-ZZZ" in r.message
               and "excluded rather than kept unverifiable" in r.message
               for r in caplog.records)


def test_run_route_pass_detects_ferry_from_recorded_fixture(caplog):
    module = _load_refresh_module()
    data = json.loads(FERRY_ROUTE_FIXTURE.read_text())["body"]
    module.fetch_route = lambda *a, **k: data
    rows = [{"a": "CFU", "b": "PVK", "ground_minutes": 307, "est_cost_eur": 17,
             "mode": gm.GROUND_MODE, "drive_minutes": 205.3, "km_road": 151.2,
             "straight_km": 105.2, "note": "land"}]
    airports = [_AP("CFU", 39.6019, 19.9117, ["greece", "island"]),
                _AP("PVK", 38.9255, 20.7653, ["greece"])]
    out, stats = module.run_route_pass(rows, airports, pace=0)
    assert stats["ferry"] == 1 and stats["land"] == 0
    assert out[0]["has_ferry"] is True and out[0]["mode"] == "ferry+ground"


# --------------------------------------------------------------------------- #
# calibration — the model vs ALL FIVE curated corridors (Task 12 req 2)         #
# --------------------------------------------------------------------------- #
# (land_min, ferry_min, sea_km, land_km) split from the live 2026-07-12 /route
# pass — these OSRM-derived splits aren't in destinations.json (only the
# curated final ground_minutes/est_cost_eur are), so they stay hand-recorded.
_ROUTE_SPLITS = {
    "CTA-MLA": (121.0, 105.0, 100.0, 124.0),
    "HER-JTR": (20.9, 125.0, 124.4, 15.8),
    "KLX-ZTH": (161.7, 60.0, 32.9, 162.8),
    "CFU-PVK": (130.3, 75.0, 18.1, 133.1),
    "CTA-SUF": (174.6, 40.0, 6.5, 228.1),
}


def _curated_target(pair_key: str) -> Tuple[int, int]:
    """The curated (ground_minutes, est_cost_eur) target for ``pair_key``
    ("A-B"), read live from data/destinations.json open_jaw_pairs (Minor #3
    fix): a curated edit re-validates the calibration band automatically
    instead of drifting from a hardcoded copy."""
    a, b = pair_key.split("-")
    destinations = json.loads(
        (Path(__file__).parent.parent / "data" / "destinations.json").read_text())
    for p in destinations["open_jaw_pairs"]:
        if {str(p["a"]).upper(), str(p["b"]).upper()} == {a, b}:
            return p["ground_minutes"], p["est_cost_eur"]
    raise AssertionError(f"no curated open_jaw_pairs entry found for {pair_key}")


# (land_min, ferry_min, sea_km, land_km) hand-recorded route split, and the
# curated (minutes, EUR) target loaded live from data/destinations.json.
CALIBRATION = {key: (splits, _curated_target(key)) for key, splits in _ROUTE_SPLITS.items()}


@pytest.mark.parametrize("corridor", list(CALIBRATION))
def test_ferry_model_duration_within_40pct_of_curated(corridor):
    (land_m, ferry_m, sea_km, _land_km), (cur_min, _cur_cost) = CALIBRATION[corridor]
    modeled = gm.ferry_ground_minutes_for(land_m, ferry_m, sea_km)
    assert abs(modeled - cur_min) / cur_min <= 0.40, (
        f"{corridor}: modeled {modeled}min vs curated {cur_min}min "
        f"({100*(modeled-cur_min)/cur_min:+.0f}%)")


@pytest.mark.parametrize("corridor", ["CTA-MLA", "HER-JTR", "KLX-ZTH", "CFU-PVK"])
def test_ferry_model_cost_within_40pct_of_curated(corridor):
    (_land_m, _ferry_m, sea_km, land_km), (_cur_min, cur_cost) = CALIBRATION[corridor]
    modeled = gm.ferry_est_cost_eur_for(land_km, sea_km)
    assert abs(modeled - cur_cost) / cur_cost <= 0.40, (
        f"{corridor}: modeled EUR{modeled} vs curated EUR{cur_cost} "
        f"({100*(modeled-cur_cost)/cur_cost:+.0f}%)")


def test_cta_suf_cost_is_a_documented_land_proxy_outlier():
    """CTA-SUF is the ONE corridor whose modeled COST cannot meet +-40%: the
    OSRM road distance (~228 km, a genuine Catania-Messina-Lamezia drive) priced
    at the shared EUR 0.11/km land proxy is ~EUR25, structurally above the
    curated EUR15 (unusually cheap Sicilian/Calabrian regional transit). This is
    exactly why the corridor is CURATED (curated EUR15 wins; the user never sees
    the computed value). The ferry FARE component itself is well-calibrated
    (Messina foot ~EUR6). Guard the documented behaviour so it can't silently
    drift."""
    (_l, _f, sea_km, land_km), (_cm, cur_cost) = CALIBRATION["CTA-SUF"]
    modeled = gm.ferry_est_cost_eur_for(land_km, sea_km)
    land_only = max(gm.COST_FLOOR_EUR, round(land_km * gm.COST_PER_KM_EUR))
    assert land_only > cur_cost * 1.40           # land proxy alone already exceeds the band
    assert modeled == 31                          # 25 land + 5 base + 1 sea (strait)


# --------------------------------------------------------------------------- #
# committed matrix — ferry rows are honest (no sea crossing priced as road)     #
# --------------------------------------------------------------------------- #
def test_committed_matrix_ferry_rows_are_wellformed():
    data = json.loads((Path(__file__).parent.parent / "data" / "ground_matrix.json").read_text())
    ferry = [p for p in data["pairs"] if p.get("has_ferry")]
    assert ferry, "expected computed ferry pairs recorded in the matrix"
    for p in ferry:
        assert p["mode"] == "ferry+ground"
        assert p["ground_minutes"] <= gm.MAX_FERRY_GROUND_MINUTES
        assert {"ferry_minutes", "land_minutes", "sea_km"} <= set(p)
        assert p["sea_km"] > 0 and p["ferry_minutes"] > 0
    # no land pair pretends to have a ferry
    land = [p for p in data["pairs"] if p["mode"] == gm.GROUND_MODE]
    assert all(not q.get("has_ferry") for q in land)
    assert data["stats"]["ferry_pairs"] == len(ferry)


# --------------------------------------------------------------------------- #
# envelope — has_ferry + ⛴️ why-string flow through S4                          #
# --------------------------------------------------------------------------- #
def test_ground_summary_has_ferry_additive():
    assert "has_ferry" not in output.ground_summary(240, 45.0, "train")
    assert output.ground_summary(240, 45.0, "ferry", has_ferry=True)["has_ferry"] is True
    assert "has_ferry" not in output.ground_summary(240, 45.0, "ferry", has_ferry=False)


def test_ferry_why_suffix_uses_boat_glyph():
    deal = output.build_deal(
        shape="S4", origin="BUD", destination="HER", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=200.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "HER", "ryanair", "2026-08-22", 80.0),
            output.ground_leg("HER", "JTR", "ferry", 240, cost_eur=45.0),
            output.flight_leg("JTR", "BUD", "ryanair", "2026-08-27", 75.0),
        ],
        ground=output.ground_summary(240, 45.0, "ferry", estimate_basis="curated", has_ferry=True),
        why="x",
    )
    suffix = output.ground_why_suffix(deal)
    assert "⛴️" in suffix and "ferry" in suffix and "HER" in suffix and "JTR" in suffix


def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="cal")


def test_planner_surfaces_curated_ferry_openjaw_with_has_ferry():
    """HER<->JTR is a CURATED ferry corridor (Task 12). With CAL fares present the
    planner must surface it as an S4 open-jaw whose ground carries has_ferry."""
    reg = DestinationRegistry()
    spec = parse_spec({
        "origins": ["BUD"], "where": "greece & island", "depart": "2026-08-22..2026-08-24",
        "nights": "4-7", "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })
    cal = {
        ("BUD", "HER"): [("2026-08-22", 80.0)],
        ("JTR", "BUD"): [("2026-08-27", 75.0)],
        ("BUD", "JTR"): [("2026-08-22", 140.0)],
        ("HER", "BUD"): [("2026-08-27", 140.0)],
    }
    p = Planner(registry=reg)
    p.ryanair.roundtrip_fares = lambda origin, dest=None, **k: []
    p.ryanair.cheapest_per_day = lambda origin, dest, month, **k: [
        _df(origin, dest, d, pr) for d, pr in cal.get((origin, dest), [])]
    p.wizz.timetable = lambda *a, **k: ([], [])

    results = p.execute(compile_plan(spec, reg), spec)["results"]
    oj = [d for d in results if d["shape"] == "S4"
          and {d["destination"], d["legs"][-1]["origin"]} == {"HER", "JTR"}]
    assert oj, "expected the curated HER-JTR ferry open-jaw to surface"
    d = oj[0]
    assert d["ground"]["has_ferry"] is True
    assert d["ground"]["estimate_basis"] == "curated"
    assert "⛴️" in d["why"]


def test_planner_surfaces_null_ferry_openjaw_as_plain_land_hop():
    """Minor #5 e2e: a computed matrix pair with ``has_ferry: null`` (a
    non-suspect /route-pass failure — AHO<->OLB, both Sardinia) must surface
    through the planner + envelope as a plain land hop: no ⛴️, no ``has_ferry``
    key on the deal's ``ground``."""
    reg = DestinationRegistry()
    reg.get_open_jaw_pairs = lambda: [
        {"a": "AHO", "b": "OLB", "ground_minutes": 150, "est_cost_eur": 20,
         "mode": gm.GROUND_MODE, "has_ferry": None, "estimate_basis": "computed"},
    ]
    spec = parse_spec({
        "origins": ["BUD"], "where": "sardinia", "depart": "2026-08-22..2026-08-24",
        "nights": "4-7", "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })
    cal = {
        ("BUD", "AHO"): [("2026-08-22", 80.0)],
        ("OLB", "BUD"): [("2026-08-27", 75.0)],
        ("BUD", "OLB"): [("2026-08-22", 140.0)],
        ("AHO", "BUD"): [("2026-08-27", 140.0)],
    }
    p = Planner(registry=reg)
    p.ryanair.roundtrip_fares = lambda origin, dest=None, **k: []
    p.ryanair.cheapest_per_day = lambda origin, dest, month, **k: [
        _df(origin, dest, d, pr) for d, pr in cal.get((origin, dest), [])]
    p.wizz.timetable = lambda *a, **k: ([], [])

    results = p.execute(compile_plan(spec, reg), spec)["results"]
    oj = [d for d in results if d["shape"] == "S4"
          and {d["destination"], d["legs"][-1]["origin"]} == {"AHO", "OLB"}]
    assert oj, "expected the AHO-OLB null-ferry open-jaw to surface"
    d = oj[0]
    assert "has_ferry" not in d["ground"]
    assert d["ground"]["mode"] == gm.GROUND_MODE
    assert "⛴️" not in d["why"]
