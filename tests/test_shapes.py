"""Task 10 — S3 extended-origin + S4 open-jaw combiner correctness.

All fixture-mocked (no network): the planner's provider methods are replaced
with deterministic tables so the pairing, dedup, ground math, why-strings and
confirm path are asserted against known-cheapest combos — including the required
case where open-jaw genuinely beats direct.
"""

from datetime import date

import pytest

from flight_deals.engine import confirm as confirm_mod
from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare, FareLeg, FarePair


# --------------------------------------------------------------------------- #
# builders                                                                     #
# --------------------------------------------------------------------------- #
def _fp(origin, dest, total, out="2026-08-22", ret="2026-08-27"):
    n = (date.fromisoformat(ret) - date.fromisoformat(out)).days
    return FarePair(
        origin=origin, destination=dest, out_date=out, return_date=ret, nights=n,
        total_price_eur=total, currency_original="EUR", price_confidence="exact",
        carrier="ryanair", source_endpoint="rt",
        outbound=FareLeg(origin=origin, destination=dest, date=out, price_eur=round(total / 2, 2), carrier="ryanair"),
        inbound=FareLeg(origin=dest, destination=origin, date=ret, price_eur=round(total / 2, 2), carrier="ryanair"),
    )


def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="cal")


def _spec(where="italy & seaside", shapes=("direct", "extended-origin", "open-jaw")):
    return parse_spec({
        "origins": ["BUD"], "where": where, "depart": "2026-08-22..2026-08-24",
        "nights": "5-8", "shapes": list(shapes), "carriers": ["ryanair"],
    })


def _planner(rt_table=None, cal_table=None):
    p = Planner()
    rt_table = rt_table or {}
    cal_table = cal_table or {}

    def fake_rt(origin, dest=None, **k):
        return list(rt_table.get(origin, []))

    def fake_cal(origin, dest, month, **k):
        return [_df(origin, dest, d, pr) for d, pr in cal_table.get((origin, dest), [])]

    p.ryanair.roundtrip_fares = fake_rt
    p.ryanair.cheapest_per_day = fake_cal
    p.wizz.timetable = lambda *a, **k: ([], [])
    return p


def _run(spec, planner):
    plan = compile_plan(spec, planner.registry)
    return planner.execute(plan, spec)["results"]


# --------------------------------------------------------------------------- #
# compile call math                                                            #
# --------------------------------------------------------------------------- #
def test_compile_counts_shape_calls():
    """extended-origin adds one RT-ANYWHERE per extended origin; open-jaw adds
    CAL descriptors for the matched pairs; --max-calls accounts for them."""
    direct = compile_plan(_spec(shapes=("direct",)), Planner().registry)
    full = compile_plan(_spec(), Planner().registry)
    s3 = [c for c in full.calls if c.shape == "S3"]
    s4 = [c for c in full.calls if c.shape == "S4"]
    assert {c.params["origin"] for c in s3} == {"VIE", "BTS"}  # extended origins of BUD
    assert all(c.endpoint == "cheapestPerDay" for c in s4)
    assert full.estimated_calls == len(direct.calls) + len(s3) + len(s4)
    assert full.estimated_calls > direct.estimated_calls


# --------------------------------------------------------------------------- #
# S4 open-jaw beats direct (the required case)                                 #
# --------------------------------------------------------------------------- #
def test_open_jaw_beats_direct():
    # Direct NAP is €200 rt; open-jaw NAP-in/BRI-out is 30+25+35 = €90.
    rt = {"BUD": [_fp("BUD", "NAP", 200.0)], "VIE": [], "BTS": []}
    cal = {
        ("BUD", "NAP"): [("2026-08-22", 30.0)],
        ("BRI", "BUD"): [("2026-08-27", 25.0)],
        ("BUD", "BRI"): [("2026-08-22", 80.0)],
        ("NAP", "BUD"): [("2026-08-27", 80.0)],
    }
    results = _run(_spec(), _planner(rt, cal))
    oj = [d for d in results if d["shape"] == "S4"]
    assert oj, "expected an open-jaw deal"
    d = oj[0]
    assert d["origin"] == "BUD" and d["destination"] == "NAP"  # fly-in airport
    assert d["price_eur"] == 90.0
    # cheapest overall is the open-jaw, not the €200 direct
    assert results[0]["shape"] == "S4"
    assert results[0]["price_eur"] == 90.0


def test_open_jaw_picks_cheaper_of_two_directions():
    # Fly-in BRI / home NAP is cheaper (20+15+35=70) than NAP-in/BRI-out (100+..).
    cal = {
        ("BUD", "NAP"): [("2026-08-22", 100.0)],
        ("BRI", "BUD"): [("2026-08-27", 100.0)],
        ("BUD", "BRI"): [("2026-08-22", 20.0)],
        ("NAP", "BUD"): [("2026-08-27", 15.0)],
    }
    results = _run(_spec(), _planner({"BUD": [], "VIE": [], "BTS": []}, cal))
    oj = [d for d in results if d["shape"] == "S4" and "BRI" in (d["destination"], d["legs"][-1]["origin"])]
    assert oj
    d = oj[0]
    assert d["destination"] == "BRI"  # cheaper direction flies into BRI
    assert d["price_eur"] == 70.0
    assert d["legs"][-1]["origin"] == "NAP"  # home from NAP


def test_open_jaw_with_high_ground_ranks_below_cheaper_direct():
    """An open-jaw whose cheap flights are dragged up by a high ground hop must
    rank BELOW a genuinely cheaper direct round-trip — the composite total
    (fares + ground), not the bare fares, is what competes on price."""
    # Direct CTA €120 (S2). Open-jaw NAP-in/BRI-out = 60 + 60 + €35 ground = €155.
    rt = {"BUD": [_fp("BUD", "CTA", 120.0)], "VIE": [], "BTS": []}
    cal = {
        ("BUD", "NAP"): [("2026-08-22", 60.0)],
        ("BRI", "BUD"): [("2026-08-27", 60.0)],
        ("BUD", "BRI"): [("2026-08-22", 200.0)],   # other direction is far worse
        ("NAP", "BUD"): [("2026-08-27", 200.0)],
    }
    results = _run(_spec(), _planner(rt, cal))
    oj = next(d for d in results if d["shape"] == "S4")
    assert oj["price_eur"] == 155.0  # 60 + 60 + 35 ground
    # Cheapest overall is the €120 direct, NOT the ground-inflated open-jaw.
    assert results[0]["shape"] == "S2" and results[0]["destination"] == "CTA"
    assert results[0]["price_eur"] == 120.0
    assert results.index(oj) > results.index(results[0])


# --------------------------------------------------------------------------- #
# S3 extended-origin: dedup cheapest-wins vs direct                            #
# --------------------------------------------------------------------------- #
def test_extended_origin_surfaces_only_when_cheaper():
    # Direct CTA €200; via-VIE CTA fare €120 (+€42 ground = €162) beats it.
    rt = {"BUD": [_fp("BUD", "CTA", 200.0)], "VIE": [_fp("VIE", "CTA", 120.0)], "BTS": []}
    results = _run(_spec(), _planner(rt, {}))
    cta = [d for d in results if d["destination"] == "CTA"]
    assert len(cta) == 1  # S2 and S3 to CTA dedupe — cheapest wins
    assert cta[0]["shape"] == "S3"
    assert cta[0]["price_eur"] == 162.0  # 120 + 2×21


def test_direct_kept_when_extended_origin_not_cheaper():
    # Direct CTA €100; via-VIE CTA €120 fare (+€42 = €162) loses -> S2 shown.
    rt = {"BUD": [_fp("BUD", "CTA", 100.0)], "VIE": [_fp("VIE", "CTA", 120.0)], "BTS": []}
    results = _run(_spec(), _planner(rt, {}))
    cta = [d for d in results if d["destination"] == "CTA"]
    assert len(cta) == 1
    assert cta[0]["shape"] == "S2"
    assert cta[0]["price_eur"] == 100.0


# --------------------------------------------------------------------------- #
# ground math + legs + why                                                     #
# --------------------------------------------------------------------------- #
def test_s3_ground_math_and_legs():
    rt = {"BUD": [], "VIE": [_fp("VIE", "NAP", 120.0)], "BTS": []}
    d = [x for x in _run(_spec(), _planner(rt, {})) if x["shape"] == "S3"][0]
    assert d["origin"] == "BUD" and d["destination"] == "NAP"
    # 2 ground + 2 flight legs, chronological
    kinds = [l["type"] for l in d["legs"]]
    assert kinds == ["ground", "flight", "flight", "ground"]
    assert d["legs"][0]["from_iata"] == "BUD" and d["legs"][0]["to_iata"] == "VIE"
    assert d["legs"][1]["origin"] == "VIE"  # flown from VIE, not BUD
    # ground summary is the doubled (round-trip) cost/duration
    assert d["ground"]["cost_eur"] == 42.0 and d["ground"]["duration_minutes"] == 330
    assert d["price_eur"] == 162.0
    assert "BUD⇄VIE" in d["why"] and "bus" in d["why"]


def test_s4_ground_math_and_legs():
    cal = {
        ("BUD", "NAP"): [("2026-08-22", 30.0)],
        ("BRI", "BUD"): [("2026-08-27", 25.0)],
        ("BUD", "BRI"): [("2026-08-22", 80.0)],
        ("NAP", "BUD"): [("2026-08-27", 80.0)],
    }
    d = [x for x in _run(_spec(), _planner({"BUD": [], "VIE": [], "BTS": []}, cal)) if x["shape"] == "S4"][0]
    kinds = [l["type"] for l in d["legs"]]
    assert kinds == ["flight", "ground", "flight"]  # one hop
    assert d["ground"]["cost_eur"] == 35.0 and d["ground"]["mode"] == "train"
    assert d["price_eur"] == 90.0
    assert "fly into NAP" in d["why"] and "fly home from BRI" in d["why"]
    # The S4 ground cost carries the ``~`` estimate marker (like S3's).
    assert "~€35" in d["why"]
    assert d["price_confidence"] == "exact"


# --------------------------------------------------------------------------- #
# confirm path (S4 exact-date one-way re-query)                                #
# --------------------------------------------------------------------------- #
def test_s4_confirm_updates_price_from_exact_oneway():
    """S4 confirm re-queries both one-way legs on their exact dates and refines
    price_eur, retaining the month-level estimate; ground cost is preserved."""
    from flight_deals import output

    deal = output.build_deal(
        shape="S4", origin="BUD", destination="NAP", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=90.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "NAP", "ryanair", "2026-08-22", 30.0),
            output.ground_leg("NAP", "BRI", "train", 240, cost_eur=35.0),
            output.flight_leg("BRI", "BUD", "ryanair", "2026-08-27", 25.0),
        ],
        ground=output.ground_summary(240, 35.0, "train"),
        why="x",
    )

    class FakeRyanair:
        def oneway_fares(self, origin, dest, *, out_from, out_to, use_cache=True):
            # exact-date fares differ from the month estimate: 40 + 20
            price = {("BUD", "NAP"): 40.0, ("BRI", "BUD"): 20.0}[(origin, dest)]
            return [_df(origin, dest, out_from, price)]

    confirm_mod.confirm([deal], wizz=None, ryanair=FakeRyanair())
    assert deal["estimated_price_eur"] == 90.0
    assert deal["price_eur"] == 95.0  # 40 + 20 + 35 ground
    assert deal["legs"][0]["price_eur"] == 40.0
    assert deal["legs"][2]["price_eur"] == 20.0
    assert deal["price_confidence"] == "exact"  # stays exact


def test_s4_confirm_unconfirmable_keeps_estimate():
    from flight_deals import output

    deal = output.build_deal(
        shape="S4", origin="BUD", destination="NAP", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=90.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "NAP", "ryanair", "2026-08-22", 30.0),
            output.ground_leg("NAP", "BRI", "train", 240, cost_eur=35.0),
            output.flight_leg("BRI", "BUD", "ryanair", "2026-08-27", 25.0),
        ],
        ground=output.ground_summary(240, 35.0, "train"), why="x",
    )

    class NoFares:
        def oneway_fares(self, *a, **k):
            return []

    confirm_mod.confirm([deal], wizz=None, ryanair=NoFares())
    assert deal["price_eur"] == 90.0  # unchanged
    assert "estimated_price_eur" not in deal
