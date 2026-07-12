"""Task 13 — Transitous/MOTIS scheduled-transit refinement.

Fixtures-only (Global Constraint 10): the acceptance rule and best-itinerary
selection are pure and asserted exactly; the ``/plan`` parsing is exercised
against RECORDED live responses (``tests/fixtures/transitous_plan_rail.json`` =
AMS-CRL, ``transitous_plan_nocoverage.json`` = HER-JTR, captured by
``scripts/refresh_ground.py --capture-transit-*`` / the live probe). No test
hits Transitous.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flight_deals.registry import ground_matrix as gm
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare
from flight_deals import output

FIXTURES = Path(__file__).parent / "fixtures"
RAIL_FIXTURE = FIXTURES / "transitous_plan_rail.json"
NOCOV_FIXTURE = FIXTURES / "transitous_plan_nocoverage.json"
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "refresh_ground.py"


def _load_refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_ground_t13", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _AP:
    def __init__(self, iata, lat, lon, tags=None):
        self.iata, self.lat, self.lon = iata, lat, lon
        self.tags = tags or []


def _body(path):
    return json.loads(path.read_text())["body"]


# --------------------------------------------------------------------------- #
# /plan parsing — multi-itinerary best selection + mode extraction             #
# --------------------------------------------------------------------------- #
def test_best_ground_itinerary_picks_shortest_and_extracts_modes():
    module = _load_refresh_module()
    best = module.best_ground_itinerary([_body(RAIL_FIXTURE)])
    assert best is not None
    dur_sec, transfers, modes = best
    # The captured AMS-CRL rail response has three itineraries (14580/13680/15840
    # s); the shortest (13680 s = 228 min) must win.
    assert dur_sec == 13680
    assert transfers == 2
    # WALK is stripped; only the real scheduled transit modes survive, de-duped.
    assert modes == ["BUS", "HIGHSPEED_RAIL", "REGIONAL_RAIL"]


def test_best_ground_itinerary_min_across_both_slots():
    module = _load_refresh_module()
    body = _body(RAIL_FIXTURE)
    # A second "slot" response whose best is longer must not displace slot 1.
    slower = {"itineraries": [{"duration": 20000, "startTime": "2026-07-28T15:00:00Z",
                               "endTime": "2026-07-28T20:33:00Z", "transfers": 1,
                               "legs": [{"mode": "WALK"}, {"mode": "RAIL"}, {"mode": "WALK"}]}]}
    best = module.best_ground_itinerary([body, slower])
    assert best[0] == 13680


def test_best_ground_itinerary_excludes_airplane():
    module = _load_refresh_module()
    air = {"itineraries": [{"duration": 5000, "startTime": "2026-07-28T10:00:00Z",
                            "endTime": "2026-07-28T11:23:00Z", "transfers": 1,
                            "legs": [{"mode": "WALK"}, {"mode": "AIRPLANE"}, {"mode": "WALK"}]}]}
    # An air itinerary is faster but MUST be rejected (not the ground hop we model).
    assert module.best_ground_itinerary([air]) is None


def test_best_ground_itinerary_rejects_walk_only():
    module = _load_refresh_module()
    walk = {"itineraries": [{"duration": 3000, "startTime": "2026-07-28T10:00:00Z",
                             "endTime": "2026-07-28T10:50:00Z", "transfers": 0,
                             "legs": [{"mode": "WALK"}]}]}
    assert module.best_ground_itinerary([walk]) is None


def test_no_coverage_fixture_yields_none():
    module = _load_refresh_module()
    assert module.best_ground_itinerary([_body(NOCOV_FIXTURE)]) is None


def test_itin_duration_falls_back_to_timestamps():
    module = _load_refresh_module()
    itin = {"startTime": "2026-07-28T10:00:00Z", "endTime": "2026-07-28T13:48:00Z"}
    assert module._itin_duration_seconds(itin) == 13680


def test_next_tuesday_slots_deterministic():
    module = _load_refresh_module()
    # 2026-07-12 is a Sunday; +14d = 2026-07-26 (Sun) -> next Tuesday = 2026-07-28.
    slots = module.next_tuesday_slots(datetime(2026, 7, 12, tzinfo=timezone.utc))
    assert slots == ["2026-07-28T10:00:00Z", "2026-07-28T15:00:00Z"]


# --------------------------------------------------------------------------- #
# acceptance rule — effective minutes, suspect bounds (both sides), caps        #
# --------------------------------------------------------------------------- #
def _pair(modeled, transit, **extra):
    p = {"a": "AMS", "b": "CRL", "ground_minutes": modeled, "est_cost_eur": 30,
         "mode": gm.GROUND_MODE, "km_road": 200.0}
    if transit is not None:
        p["transit_minutes"] = transit
        p["transit_transfers"] = 2
        p["transit_modes"] = ["RAIL"]
    p.update(extra)
    return p


def test_accept_transit_within_bounds_surfaces_scheduled():
    out = gm.apply_transit_refinement(_pair(288, 228))
    assert out["ground_minutes"] == 228
    assert out["modeled_minutes"] == 288
    assert out["_transit_basis"] == "scheduled"
    assert out["est_cost_eur"] == 30  # fare untouched


@pytest.mark.parametrize("modeled, transit", [
    (288, 143),   # 143 < 0.5*288 (=144): too-fast suspect
    (288, 865),   # 865 > 3.0*288 (=864): too-slow suspect (also > cap, still suspect)
    (100, 49),    # low side
    (100, 301),   # high side
])
def test_suspect_bounds_both_sides_keep_modeled(modeled, transit, caplog):
    with caplog.at_level("WARNING"):
        out = gm.apply_transit_refinement(_pair(modeled, transit))
    assert out["ground_minutes"] == modeled
    assert "_transit_basis" not in out
    assert "transit_suspect" in caplog.text


def test_accept_boundary_exactly_half_and_triple():
    assert gm.apply_transit_refinement(_pair(200, 100))["_transit_basis"] == "scheduled"  # 0.5x
    assert gm.apply_transit_refinement(_pair(100, 300))["_transit_basis"] == "scheduled"  # 3.0x


def test_transit_over_land_cap_not_accepted():
    # Within 3.0x of a large modeled value but over the 330 land cap.
    out = gm.apply_transit_refinement(_pair(200, 400))
    assert out["ground_minutes"] == 200
    assert "_transit_basis" not in out


def test_ferry_pair_uses_ferry_cap():
    out = gm.apply_transit_refinement(_pair(300, 400, has_ferry=True))
    assert out["ground_minutes"] == 400  # 400 <= 420 ferry cap, within 3.0x
    assert out["_transit_basis"] == "scheduled"


def test_no_coverage_keeps_modeled_untouched():
    p = {"a": "AHO", "b": "CAG", "ground_minutes": 324, "est_cost_eur": 20,
         "mode": gm.GROUND_MODE, "transit": "no_coverage"}
    out = gm.apply_transit_refinement(p)
    assert out["ground_minutes"] == 324
    assert "_transit_basis" not in out


# --------------------------------------------------------------------------- #
# registry merge — scheduled estimate_basis + effective minutes preference       #
# --------------------------------------------------------------------------- #
def test_merge_tags_accepted_pair_scheduled():
    merged = gm.merge_open_jaw_pairs([], [_pair(288, 228)])
    assert merged[0]["estimate_basis"] == "scheduled"
    assert merged[0]["ground_minutes"] == 228
    assert merged[0]["transit_transfers"] == 2


def test_merge_tags_suspect_pair_computed():
    merged = gm.merge_open_jaw_pairs([], [_pair(288, 50)])
    assert merged[0]["estimate_basis"] == "computed"
    assert merged[0]["ground_minutes"] == 288


def test_merge_curated_never_transit_refined():
    curated = [{"a": "AMS", "b": "CRL", "ground_minutes": 999, "est_cost_eur": 40,
                "mode": "train"}]
    merged = gm.merge_open_jaw_pairs(curated, [_pair(288, 228)])
    assert len(merged) == 1  # computed dropped for same {a,b}
    assert merged[0]["estimate_basis"] == "curated"
    assert merged[0]["ground_minutes"] == 999


# --------------------------------------------------------------------------- #
# envelope / display — no ~ on scheduled DURATION, ~ kept on COST                #
# --------------------------------------------------------------------------- #
def test_ground_summary_transit_transfers_additive():
    assert "transit_transfers" not in output.ground_summary(228, 30.0, "train")
    s = output.ground_summary(228, 30.0, "train", estimate_basis="scheduled",
                              transit_transfers=2)
    assert s["transit_transfers"] == 2


def _scheduled_deal():
    return output.build_deal(
        shape="S4", origin="BUD", destination="AMS", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=180.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "AMS", "ryanair", "2026-08-22", 80.0),
            output.ground_leg("AMS", "CRL", "public_transit", 228, cost_eur=30.0),
            output.flight_leg("CRL", "BUD", "ryanair", "2026-08-27", 70.0),
        ],
        ground=output.ground_summary(228, 30.0, "public_transit",
                                     estimate_basis="scheduled", transit_transfers=2),
        why="x",
    )


def test_scheduled_why_drops_tilde_on_duration_keeps_on_cost():
    suffix = output.ground_why_suffix(_scheduled_deal())
    assert "scheduled" in suffix
    assert "3h48m" in suffix          # duration present...
    assert "~3h48m" not in suffix     # ...but with NO ~ marker
    assert "~€30" in suffix           # cost keeps the ~ (fares stay modeled)


def test_computed_why_keeps_tilde_on_both():
    deal = _scheduled_deal()
    deal["ground"]["estimate_basis"] = "computed"
    suffix = output.ground_why_suffix(deal)
    assert "~3h48m" in suffix and "~€30" in suffix and "scheduled" not in suffix


# --------------------------------------------------------------------------- #
# transit pass — cap-drop + whole-service failure                               #
# --------------------------------------------------------------------------- #
def _row(a="AMS", b="CRL", modeled=288, **extra):
    r = {"a": a, "b": b, "ground_minutes": modeled, "est_cost_eur": 30,
         "mode": gm.GROUND_MODE, "km_road": 200.0}
    r.update(extra)
    return r


def test_run_transit_pass_refines_from_recorded_fixture():
    module = _load_refresh_module()
    module.fetch_plan = lambda *a, **k: _body(RAIL_FIXTURE)
    airports = [_AP("AMS", 52.3086, 4.7639), _AP("CRL", 50.4592, 4.4538)]
    out, stats = module.run_transit_pass(_row_list([_row()]), airports, pace=0)
    assert stats["refined"] == 1 and stats["http_ok"] == 1
    assert out[0]["transit_minutes"] == 228
    assert out[0]["transit_transfers"] == 2
    assert out[0]["transit_modes"] == ["BUS", "HIGHSPEED_RAIL", "REGIONAL_RAIL"]
    assert "transit_queried_at" in out[0]


def test_run_transit_pass_no_coverage_keeps_modeled():
    module = _load_refresh_module()
    module.fetch_plan = lambda *a, **k: _body(NOCOV_FIXTURE)
    airports = [_AP("HER", 35.3397, 25.1803), _AP("JTR", 36.3992, 25.4793)]
    out, stats = module.run_transit_pass(_row_list([_row("HER", "JTR", 327)]), airports, pace=0)
    assert stats["no_coverage"] == 1
    assert out[0]["transit"] == "no_coverage"
    assert "transit_minutes" not in out[0]
    assert out[0]["ground_minutes"] == 327


def test_run_transit_pass_drops_pair_over_cap():
    module = _load_refresh_module()
    # A 8h scheduled itinerary (28800 s = 480 min) exceeds the 330-min land cap.
    far = {"itineraries": [{"duration": 28800, "startTime": "2026-07-28T10:00:00Z",
                            "endTime": "2026-07-28T18:00:00Z", "transfers": 1,
                            "legs": [{"mode": "WALK"}, {"mode": "RAIL"}, {"mode": "WALK"}]}]}
    module.fetch_plan = lambda *a, **k: far
    airports = [_AP("AMS", 52.3086, 4.7639), _AP("CRL", 50.4592, 4.4538)]
    out, stats = module.run_transit_pass(_row_list([_row()]), airports, pace=0)
    assert stats["dropped_cap"] == 1
    assert out == []  # pair dropped from the matrix (honest "too far")


def test_run_transit_pass_whole_service_failure(monkeypatch):
    module = _load_refresh_module()
    def boom(*a, **k):
        raise module.TransitousError("connection refused")
    module.fetch_plan = boom
    airports = [_AP("AMS", 52.3086, 4.7639), _AP("CRL", 50.4592, 4.4538)]
    rows = _row_list([_row()])
    out, stats = module.run_transit_pass(rows, airports, pace=0)
    assert stats["http_ok"] == 0          # signals whole-service failure to main()
    assert stats["errors"] == 1
    # The row is returned unrefined (modeled intact) — the matrix stays valid.
    assert out[0]["ground_minutes"] == 288
    assert "transit_minutes" not in out[0]


def _row_list(rows):
    # run_transit_pass mutates via dict(row); pass a fresh list each call.
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# e2e — an S4 open-jaw with a scheduled matrix pair                             #
# --------------------------------------------------------------------------- #
def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="cal")


def test_e2e_s4_scheduled_pair_surfaces_scheduled_basis(tmp_path):
    matrix = {
        "schema_version": 1, "computed_at": "2026-07-12T00:00:00+00:00",
        "model": dict(gm.MODEL_PARAMS), "stats": {},
        "airports_seen": ["AMS", "CRL"],
        "pairs": [{
            "a": "AMS", "b": "CRL", "ground_minutes": 288, "est_cost_eur": 30,
            "mode": gm.GROUND_MODE, "km_road": 240.0,
            "transit_minutes": 228, "transit_transfers": 2,
            "transit_modes": ["BUS", "HIGHSPEED_RAIL", "REGIONAL_RAIL"],
            "transit_queried_at": "2026-07-12T00:00:00+00:00",
        }],
    }
    mpath = tmp_path / "gm.json"
    mpath.write_text(json.dumps(matrix))
    reg = DestinationRegistry(ground_matrix_path=str(mpath))
    spec = parse_spec({
        "origins": ["BUD"], "where": "netherlands | belgium",
        "depart": "2026-08-22..2026-08-24", "nights": "4-7",
        "shapes": ["direct", "open-jaw"], "carriers": ["ryanair"],
    })
    cal = {
        ("BUD", "AMS"): [("2026-08-22", 80.0)],
        ("CRL", "BUD"): [("2026-08-27", 70.0)],
        ("BUD", "CRL"): [("2026-08-22", 130.0)],
        ("AMS", "BUD"): [("2026-08-27", 130.0)],
    }
    p = Planner(registry=reg)
    p.ryanair.roundtrip_fares = lambda origin, dest=None, **k: []
    p.ryanair.cheapest_per_day = lambda origin, dest, month, **k: [
        _df(origin, dest, d, pr) for d, pr in cal.get((origin, dest), [])]
    p.wizz.timetable = lambda *a, **k: ([], [])

    results = p.execute(compile_plan(spec, reg), spec)["results"]
    oj = [d for d in results if d["shape"] == "S4"
          and {d["destination"], d["legs"][-1]["origin"]} == {"AMS", "CRL"}]
    assert oj, "expected the AMS-CRL scheduled open-jaw to surface"
    d = oj[0]
    assert d["ground"]["estimate_basis"] == "scheduled"
    assert d["ground"]["duration_minutes"] == 228     # effective scheduled minutes
    assert d["ground"]["transit_transfers"] == 2
    assert "scheduled" in d["why"] and "~€30" in d["why"]
