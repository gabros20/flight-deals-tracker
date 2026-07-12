"""Task 14 — City-anchor hybrid transit refinement.

Fixtures-only (Global Constraint 10). Three layers are exercised:

* the WRITE path (``scripts/refresh_ground.py``): pad arithmetic
  (``access_pad_for``), the hybrid pass (``run_hybrid_pass``) against a RECORDED
  ``/plan`` body — refined, no_coverage, no-anchor, cap-drop, suspect,
  skip-already-refined, whole-pass failure;
* the READ path (``registry.ground_matrix``): hybrid acceptance + the
  ``scheduled > scheduled-hybrid > modeled`` precedence, bounds/caps, merge tag;
* the envelope/display (``output``): hybrid KEEPS ``~`` on duration (pads are
  modeled) but says "line-haul scheduled", and the e2e S4 open-jaw.

Plus the registry city-anchor validation suite (req 1): coords in-range, anchor
within 150 km of its airport, multi-airport groups share one anchor, pads are
positive ints. No test hits Transitous.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare
from flight_deals import output

FIXTURES = Path(__file__).parent / "fixtures"
RAIL_FIXTURE = FIXTURES / "transitous_plan_rail.json"       # AMS-CRL, 228-min best
NOCOV_FIXTURE = FIXTURES / "transitous_plan_nocoverage.json"
HYBRID_FIXTURE = FIXTURES / "transitous_plan_hybrid.json"   # live city-anchor (BUD-VIE)
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "refresh_ground.py"


def _load_refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_ground_t14", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _AP:
    """Duck-typed airport with the city anchor + pad fields the hybrid pass reads."""

    def __init__(self, iata, lat, lon, city_lat, city_lon, access_pad_minutes=None):
        self.iata, self.lat, self.lon = iata, lat, lon
        self.city_lat, self.city_lon = city_lat, city_lon
        self.access_pad_minutes = access_pad_minutes
        self.tags = []


def _body(path):
    return json.loads(path.read_text())["body"]


def _row(a="BUD", b="VIE", modeled=263, **extra):
    r = {"a": a, "b": b, "ground_minutes": modeled, "est_cost_eur": 25,
         "mode": gm.GROUND_MODE, "km_road": 240.0, "transit": "no_coverage"}
    r.update(extra)
    return r


def _airports():
    # BUD pad override 40, VIE pad override 35; city anchors near the airports.
    return [_AP("BUD", 47.4369, 19.2556, 47.4979, 19.0402, 40),
            _AP("VIE", 48.1103, 16.5697, 48.2082, 16.3738, 35)]


# --------------------------------------------------------------------------- #
# pad arithmetic — override vs default                                          #
# --------------------------------------------------------------------------- #
def test_access_pad_override_and_default():
    module = _load_refresh_module()
    assert module.access_pad_for(_AP("BVA", 0, 0, 0, 0, 75)) == 75      # override
    assert module.access_pad_for(_AP("CFU", 0, 0, 0, 0)) == 30          # model default
    assert module.access_pad_for(_AP("X", 0, 0, 0, 0, 0)) == 30         # 0/invalid -> default
    assert module.gm.ACCESS_PAD_MINUTES == 30


# --------------------------------------------------------------------------- #
# hybrid pass (WRITE path) — refined, no_coverage, no-anchor, cap, suspect       #
# --------------------------------------------------------------------------- #
def test_run_hybrid_pass_refines_with_pads():
    module = _load_refresh_module()
    module.fetch_plan = lambda *a, **k: _body(RAIL_FIXTURE)  # 228-min line-haul
    out, stats = module.run_hybrid_pass(
        [_row()], _airports(), slots=["s1", "s2"], pace=0)
    assert stats["candidates"] == 1 and stats["refined"] == 1 and stats["http_ok"] == 1
    # hybrid = pad_a(40) + line-haul(228) + pad_b(35) = 303
    assert out[0]["transit_hybrid_minutes"] == 303
    assert out[0]["linehaul_minutes"] == 228
    assert out[0]["transit_hybrid_transfers"] == 2
    assert out[0]["transit_hybrid_modes"] == ["BUS", "HIGHSPEED_RAIL", "REGIONAL_RAIL"]
    assert "transit_hybrid_queried_at" in out[0]
    # the pure-pass no_coverage marker stays (a factual record of the pure result)
    assert out[0]["transit"] == "no_coverage"
    # modeled values untouched
    assert out[0]["ground_minutes"] == 263 and out[0]["est_cost_eur"] == 25


def test_run_hybrid_pass_skips_already_refined_rows():
    module = _load_refresh_module()
    called = []
    module.fetch_plan = lambda *a, **k: called.append(1) or _body(RAIL_FIXTURE)
    # A row the PURE pass already refined (transit_minutes present, no no_coverage).
    pure = _row("AMS", "CRL"); pure.pop("transit"); pure["transit_minutes"] = 228
    out, stats = module.run_hybrid_pass([pure], _airports(), slots=["s1"], pace=0)
    assert stats["candidates"] == 0 and not called   # never queried
    assert "transit_hybrid_minutes" not in out[0]


def test_run_hybrid_pass_no_anchor_stays_no_coverage():
    module = _load_refresh_module()
    module.fetch_plan = lambda *a, **k: _body(RAIL_FIXTURE)
    aps = [_AP("BUD", 47.4, 19.2, None, None, 40), _AP("VIE", 48.1, 16.5, 48.2, 16.3, 35)]
    out, stats = module.run_hybrid_pass([_row()], aps, slots=["s1"], pace=0)
    assert stats["no_anchor"] == 1
    assert "transit_hybrid_minutes" not in out[0]
    assert out[0]["transit"] == "no_coverage"


def test_run_hybrid_pass_no_ground_itinerary_stays_no_coverage():
    module = _load_refresh_module()
    module.fetch_plan = lambda *a, **k: _body(NOCOV_FIXTURE)
    out, stats = module.run_hybrid_pass([_row()], _airports(), slots=["s1"], pace=0)
    assert stats["no_coverage"] == 1
    assert "transit_hybrid_minutes" not in out[0]
    assert out[0]["transit"] == "no_coverage"


def test_run_hybrid_pass_drops_pair_over_cap():
    module = _load_refresh_module()
    # 8h line-haul (480 min) + 75 pads => 555 > 330 land cap -> dropped.
    far = {"itineraries": [{"duration": 28800, "startTime": "2026-07-28T10:00:00Z",
                            "endTime": "2026-07-28T18:00:00Z", "transfers": 1,
                            "legs": [{"mode": "WALK"}, {"mode": "RAIL"}, {"mode": "WALK"}]}]}
    module.fetch_plan = lambda *a, **k: far
    out, stats = module.run_hybrid_pass([_row()], _airports(), slots=["s1"], pace=0)
    assert stats["dropped_cap"] == 1
    assert out == []


def test_run_hybrid_pass_suspect_keeps_annotation_but_flags(caplog):
    module = _load_refresh_module()
    # A 15-min line-haul + 75 pads = 90 min hybrid, far below 0.5*263 (131.5) -> suspect.
    fast = {"itineraries": [{"duration": 900, "startTime": "2026-07-28T10:00:00Z",
                             "endTime": "2026-07-28T10:15:00Z", "transfers": 0,
                             "legs": [{"mode": "WALK"}, {"mode": "RAIL"}, {"mode": "WALK"}]}]}
    module.fetch_plan = lambda *a, **k: fast
    with caplog.at_level("WARNING"):
        out, stats = module.run_hybrid_pass([_row()], _airports(), slots=["s1"], pace=0)
    assert stats["suspect"] == 1
    assert out[0]["transit_hybrid_minutes"] == 90   # stored (read path rejects it)
    assert "hybrid_suspect" in caplog.text


def test_run_hybrid_pass_whole_pass_failure_signalled():
    module = _load_refresh_module()
    def boom(*a, **k):
        raise module.TransitousError("refused")
    module.fetch_plan = boom
    out, stats = module.run_hybrid_pass([_row()], _airports(), slots=["s1", "s2"], pace=0)
    assert stats["http_ok"] == 0 and stats["candidates"] == 1 and stats["errors"] == 1
    assert out[0]["ground_minutes"] == 263          # matrix stays valid
    assert "transit_hybrid_minutes" not in out[0]


# --------------------------------------------------------------------------- #
# read-path acceptance — precedence scheduled > hybrid > modeled, bounds/caps    #
# --------------------------------------------------------------------------- #
def _rpair(modeled, *, hybrid=None, pure=None, has_ferry=False):
    p = {"a": "BUD", "b": "VIE", "ground_minutes": modeled, "est_cost_eur": 25,
         "mode": gm.GROUND_MODE, "transit": "no_coverage", "has_ferry": has_ferry}
    if hybrid is not None:
        p["transit_hybrid_minutes"] = hybrid
        p["transit_hybrid_transfers"] = 1
    if pure is not None:
        p.pop("transit", None)
        p["transit_minutes"] = pure
        p["transit_transfers"] = 3
    return p


def test_hybrid_accepted_within_bounds():
    out = gm.apply_transit_refinement(_rpair(263, hybrid=225))
    assert out["ground_minutes"] == 225
    assert out["modeled_minutes"] == 263
    assert out["_transit_basis"] == "scheduled-hybrid"
    assert out["est_cost_eur"] == 25                # fare untouched


@pytest.mark.parametrize("modeled, hybrid", [
    (263, 130),   # 130 < 0.5*263 (131.5) -> too-fast suspect
    (100, 400),   # 400 > 3.0*100 and > cap -> suspect
])
def test_hybrid_suspect_keeps_modeled(modeled, hybrid, caplog):
    with caplog.at_level("WARNING"):
        out = gm.apply_transit_refinement(_rpair(modeled, hybrid=hybrid))
    assert out["ground_minutes"] == modeled
    assert "_transit_basis" not in out
    assert "transit_suspect" in caplog.text and "hybrid" in caplog.text


def test_hybrid_over_land_cap_not_accepted():
    # within 3.0x of a large modeled value but over the 330 land cap
    out = gm.apply_transit_refinement(_rpair(200, hybrid=400))
    assert out["ground_minutes"] == 200 and "_transit_basis" not in out


def test_hybrid_ferry_cap_allows_up_to_420():
    out = gm.apply_transit_refinement(_rpair(300, hybrid=400, has_ferry=True))
    assert out["ground_minutes"] == 400 and out["_transit_basis"] == "scheduled-hybrid"


def test_precedence_pure_scheduled_wins_over_hybrid():
    # both present and both in-bounds: pure scheduled takes precedence
    out = gm.apply_transit_refinement(_rpair(263, hybrid=225, pure=210))
    assert out["ground_minutes"] == 210 and out["_transit_basis"] == "scheduled"


def test_precedence_pure_suspect_does_not_fall_through_to_hybrid():
    # pure present but suspect (too fast); hybrid in-bounds but must NOT be used
    out = gm.apply_transit_refinement(_rpair(263, hybrid=225, pure=50))
    assert out["ground_minutes"] == 263 and "_transit_basis" not in out


def test_merge_tags_hybrid_pair_scheduled_hybrid():
    merged = gm.merge_open_jaw_pairs([], [_rpair(263, hybrid=225)])
    assert merged[0]["estimate_basis"] == "scheduled-hybrid"
    assert merged[0]["ground_minutes"] == 225
    assert merged[0]["transit_hybrid_transfers"] == 1


# --------------------------------------------------------------------------- #
# envelope / display — hybrid KEEPS ~ on duration, says "line-haul scheduled"    #
# --------------------------------------------------------------------------- #
def _hybrid_deal():
    return output.build_deal(
        shape="S4", origin="BUD", destination="VIE", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=180.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "VIE", "ryanair", "2026-08-22", 80.0),
            output.ground_leg("VIE", "BTS", "public_transit", 225, cost_eur=25.0),
            output.flight_leg("BTS", "BUD", "ryanair", "2026-08-27", 70.0),
        ],
        ground=output.ground_summary(225, 25.0, "public_transit",
                                     estimate_basis="scheduled-hybrid", transit_transfers=1),
        why="x",
    )


def test_hybrid_why_keeps_tilde_on_duration_and_cost():
    suffix = output.ground_why_suffix(_hybrid_deal())
    assert "line-haul scheduled" in suffix
    assert "~3h45m" in suffix          # duration KEEPS the ~ (pads modeled)
    assert "~€25" in suffix            # cost keeps the ~ (fares modeled)


def test_hybrid_ground_summary_carries_basis_and_transfers():
    s = output.ground_summary(225, 25.0, "public_transit",
                              estimate_basis="scheduled-hybrid", transit_transfers=1)
    assert s["estimate_basis"] == "scheduled-hybrid"
    assert s["transit_transfers"] == 1


# --------------------------------------------------------------------------- #
# e2e — an S4 open-jaw backed by a hybrid matrix pair                            #
# --------------------------------------------------------------------------- #
def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="cal")


def test_e2e_s4_hybrid_pair_surfaces_scheduled_hybrid_basis(tmp_path):
    matrix = {
        "schema_version": 1, "computed_at": "2026-07-12T00:00:00+00:00",
        "model": dict(gm.MODEL_PARAMS), "stats": {},
        "airports_seen": ["BTS", "VIE"],
        "pairs": [{
            "a": "BTS", "b": "VIE", "ground_minutes": 111, "est_cost_eur": 12,
            "mode": gm.GROUND_MODE, "km_road": 80.0, "transit": "no_coverage",
            "transit_hybrid_minutes": 130, "transit_hybrid_transfers": 1,
            "transit_hybrid_modes": ["REGIONAL_RAIL"],
            "linehaul_minutes": 60,
            "transit_hybrid_queried_at": "2026-07-12T00:00:00+00:00",
        }],
    }
    mpath = tmp_path / "gm.json"
    mpath.write_text(json.dumps(matrix))
    reg = DestinationRegistry(ground_matrix_path=str(mpath))
    spec = parse_spec({
        "origins": ["BUD"], "where": "austria | slovakia",
        "depart": "2026-08-22..2026-08-24", "nights": "4-7",
        "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })
    cal = {
        ("BUD", "VIE"): [("2026-08-22", 80.0)],
        ("BTS", "BUD"): [("2026-08-27", 70.0)],
        ("BUD", "BTS"): [("2026-08-22", 130.0)],
        ("VIE", "BUD"): [("2026-08-27", 130.0)],
    }
    p = Planner(registry=reg)
    p.ryanair.roundtrip_fares = lambda origin, dest=None, **k: []
    p.ryanair.cheapest_per_day = lambda origin, dest, month, **k: [
        _df(origin, dest, d, pr) for d, pr in cal.get((origin, dest), [])]
    p.wizz.timetable = lambda *a, **k: ([], [])

    results = p.execute(compile_plan(spec, reg), spec)["results"]
    oj = [d for d in results if d["shape"] == "S4"
          and {d["destination"], d["legs"][-1]["origin"]} == {"BTS", "VIE"}]
    assert oj, "expected the BTS-VIE hybrid open-jaw to surface"
    d = oj[0]
    assert d["ground"]["estimate_basis"] == "scheduled-hybrid"
    assert d["ground"]["duration_minutes"] == 130     # effective hybrid minutes
    assert d["ground"]["transit_transfers"] == 1
    assert "line-haul scheduled" in d["why"] and "~€12" in d["why"]


# --------------------------------------------------------------------------- #
# recorded live city-anchor fixture — parse it exactly (req 6)                   #
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HYBRID_FIXTURE.exists(),
                    reason="hybrid fixture recorded during the live --transit run")
def test_hybrid_fixture_parses_to_a_ground_itinerary():
    module = _load_refresh_module()
    best = module.best_ground_itinerary([_body(HYBRID_FIXTURE)])
    assert best is not None
    dur_sec, transfers, modes = best
    assert dur_sec > 0 and modes and "AIRPLANE" not in modes


# --------------------------------------------------------------------------- #
# registry city-anchor validation suite (req 1)                                 #
# --------------------------------------------------------------------------- #
def _registry_airports():
    return DestinationRegistry().airports


def test_every_airport_has_city_anchor_in_range():
    for a in _registry_airports():
        assert a.city_lat is not None and a.city_lon is not None, f"{a.iata} lacks anchor"
        assert -90 <= a.city_lat <= 90, f"{a.iata} city_lat out of range"
        assert -180 <= a.city_lon <= 180, f"{a.iata} city_lon out of range"


def test_city_anchor_within_150km_of_airport():
    for a in _registry_airports():
        d = gm.haversine_km(a.lat, a.lon, a.city_lat, a.city_lon)
        assert d <= 150, f"{a.iata} city anchor {d:.0f} km from airport (> 150 km)"


def test_multi_airport_groups_share_identical_anchor():
    reg = DestinationRegistry()
    by_iata = {a.iata: a for a in reg.airports}
    for city, iatas in reg.multi_city.items():
        anchors = {(by_iata[i].city_lat, by_iata[i].city_lon)
                   for i in iatas if i in by_iata}
        assert len(anchors) == 1, f"{city} airports {iatas} do not share one anchor: {anchors}"


def test_access_pad_overrides_are_positive_ints():
    for a in _registry_airports():
        if a.access_pad_minutes is not None:
            assert isinstance(a.access_pad_minutes, int) and not isinstance(a.access_pad_minutes, bool)
            assert a.access_pad_minutes > 0, f"{a.iata} pad must be positive"


def test_expected_pad_overrides_present():
    by_iata = {a.iata: a for a in _registry_airports()}
    expected = {"BVA": 75, "STN": 55, "LTN": 50, "CRL": 55, "BGY": 40, "MXP": 50,
                "BUD": 40, "VIE": 35, "FCO": 45, "CIA": 45, "BER": 35, "MAD": 35,
                "BCN": 35}
    for iata, pad in expected.items():
        assert by_iata[iata].access_pad_minutes == pad, f"{iata} pad != {pad}"
