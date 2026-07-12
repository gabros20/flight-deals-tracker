"""Task 16 — S5 via-hub self-transfer.

Fixture-mocked (no network): the pure MCT/composition logic is unit-tested on
synthetic DayFares carrying real farfnd-shaped datetimes (incl. an overnight
arrival), and the full discover→verify funnel is driven through the Planner with
a fake Ryanair provider. The two things that CANNOT be wrong get the most cover:
the MCT/datetime math and the unverified-never-shown rule.
"""

from datetime import date

import pytest

from flight_deals.engine import via_hub as vh
from flight_deals.engine.planner import Planner, compile_plan, resolve_hubs
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare
from flight_deals.output import deal_id


# --------------------------------------------------------------------------- #
# builders                                                                     #
# --------------------------------------------------------------------------- #
def _df(origin, dest, day, price, *, dep=None, arr=None):
    """A one-way DayFare with full airport-local datetimes (dep/arr are HH:MM on
    ``day`` unless they carry their own date, e.g. an overnight '2026-08-23T00:05')."""
    def _at(t):
        if t is None:
            return None
        return t if "T" in t else f"{day}T{t}:00"
    return DayFare(
        origin=origin, destination=dest, date=day, price_eur=price,
        currency_original="EUR", price_confidence="exact", carrier="ryanair",
        source_endpoint="farfnd/oneWayFares",
        departure_time=(dep if dep and "T" not in dep else (dep or None)),
        departure_at=_at(dep), arrival_at=_at(arr),
    )


# --------------------------------------------------------------------------- #
# PURE: connect math + MCT boundaries (179 / 180 / 481) + overnight            #
# --------------------------------------------------------------------------- #
def test_connect_minutes_basic():
    assert vh.connect_minutes("2026-08-22T09:30:00", "2026-08-22T13:00:00") == 210


def test_connect_minutes_overnight_uses_full_datetime_not_date_math():
    # Land 23:40, depart 03:10 NEXT day -> 3h30 = 210 min, NOT a negative from
    # subtracting bare times.
    assert vh.connect_minutes("2026-08-22T23:40:00", "2026-08-23T03:10:00") == 210


def test_connect_minutes_none_when_missing():
    assert vh.connect_minutes(None, "2026-08-22T13:00:00") is None
    assert vh.connect_minutes("2026-08-22T13:00:00", None) is None


def test_mct_boundaries_179_180_481():
    assert vh.mct_ok(180, 180, 480) is True   # exactly the floor passes
    assert vh.mct_ok(179, 180, 480) is False  # one minute short fails
    assert vh.mct_ok(480, 180, 480) is True   # exactly the ceiling passes
    assert vh.mct_ok(481, 180, 480) is False  # one minute over is a stopover
    assert vh.mct_ok(-30, 180, 480) is False  # impossible connection
    assert vh.mct_ok(None, 180, 480) is False


# --------------------------------------------------------------------------- #
# PURE: discovery composition — same-day only + MCT-plausible                  #
# --------------------------------------------------------------------------- #
def test_discover_same_day_mct_plausible_only():
    origin_fares = [_df("BUD", "VIE", "2026-08-22", 30, dep="08:00", arr="09:30")]
    hub_fares = {"VIE": [
        _df("VIE", "LIS", "2026-08-22", 40, dep="13:00", arr="16:00"),  # +210 min OK
        _df("VIE", "FAO", "2026-08-23", 20, dep="13:00", arr="16:00"),  # DIFFERENT day -> drop
        _df("VIE", "AGP", "2026-08-22", 20, dep="10:00", arr="12:00"),  # +30 min -> below MCT
    ]}
    got = vh.discover("BUD", ["VIE"], origin_fares, hub_fares, {"LIS", "FAO", "AGP"},
                      min_connect=180, max_connect=480)
    assert [(c.destination, c.connect_out_minutes) for c in got] == [("LIS", 210)]
    assert got[0].out_price_eur == 70.0


def test_discover_excludes_hub_and_origin_as_dest():
    origin_fares = [_df("BUD", "VIE", "2026-08-22", 30, dep="08:00", arr="09:30")]
    hub_fares = {"VIE": [
        _df("VIE", "BUD", "2026-08-22", 40, dep="13:00", arr="15:00"),  # back to origin
        _df("VIE", "BGY", "2026-08-22", 40, dep="13:00", arr="15:00"),  # another hub
    ]}
    got = vh.discover("BUD", ["VIE", "BGY"], origin_fares, hub_fares,
                      {"BUD", "BGY", "LIS"}, min_connect=180, max_connect=480)
    assert got == []


def test_shortlist_caps_and_reports_drops():
    cands = [vh.DiscoveredS5("VIE", f"D{i}", "2026-08-22", float(i), 200) for i in range(9)]
    kept, dropped = vh.shortlist(cands, size=6)
    assert len(kept) == 6 and dropped == 3
    assert [c.out_price_eur for c in kept] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]  # cheapest first


# --------------------------------------------------------------------------- #
# End-to-end funnel through the Planner (fake provider)                        #
# --------------------------------------------------------------------------- #
class FakeRyanair:
    """Serves the anywhere discovery sweeps (dest=None), the return-window CAL
    calendars (cheapest_per_day), and the exact-date verification legs
    (out_from==out_to). ``exact`` maps (origin,dest,day) -> a DayFare (missing key
    = 'unbookable that day'); ``cal`` maps (origin,dest,month) -> list[DayFare]."""

    def __init__(self, anywhere, exact, cal=None):
        self.anywhere = anywhere
        self.exact = exact
        self.cal = cal or {}
        self.exact_calls = []
        self.cal_calls = []

    def oneway_fares(self, origin, dest=None, *, out_from, out_to, use_cache=True):
        if dest is None:
            return list(self.anywhere.get(origin, []))
        self.exact_calls.append((origin, dest, out_from))
        f = self.exact.get((origin, dest, out_from))
        return [f] if f else []

    def roundtrip_fares(self, *a, **k):
        return []

    def cheapest_per_day(self, origin, dest, month, *, use_cache=True):
        self.cal_calls.append((origin, dest, month))
        return list(self.cal.get((origin, dest, month), []))


def _planner(anywhere, exact, cal=None, via=None):
    p = Planner()
    p.ryanair = FakeRyanair(anywhere, exact, cal)
    p.wizz.timetable = lambda *a, **k: ([], [])
    return p


def _spec(shapes=("via-hub",), via=None):
    d = {"origins": ["BUD"], "where": "portugal | spain", "depart": "2026-08-22..2026-08-24",
         "nights": "4-7", "shapes": list(shapes), "carriers": ["ryanair"]}
    if via is not None:
        d["via"] = via
    return parse_spec(d)


def _cal(origin, dest, day, price):
    """A day-level CAL DayFare (no times — cheapestPerDay carries none)."""
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="exact", carrier="ryanair",
                   source_endpoint="farfnd/oneWayFares/cheapestPerDay")


# A verifiable BUD->VIE->LIS self-transfer. Outbound fixed at 08-22 from
# discovery; the return-window sweep (nights 4-7 -> return window 08-26..08-29)
# picks the cheapest return date where BOTH legs fly: 08-26 (45+25=70), which
# then time-verifies. The 08-26 return keeps the frozen S5 deal_id golden.
def _good_tables():
    anywhere = {
        "BUD": [_df("BUD", "VIE", "2026-08-22", 30, dep="08:00", arr="09:30")],
        "VIE": [_df("VIE", "LIS", "2026-08-22", 40, dep="13:00", arr="16:00")],
    }
    cal = {
        ("LIS", "VIE", "2026-08"): [
            _cal("LIS", "VIE", "2026-08-26", 45),   # cheapest combined return
            _cal("LIS", "VIE", "2026-08-28", 50),
        ],
        ("VIE", "BUD", "2026-08"): [
            _cal("VIE", "BUD", "2026-08-26", 25),
            _cal("VIE", "BUD", "2026-08-28", 28),
        ],
    }
    exact = {
        ("BUD", "VIE", "2026-08-22"): _df("BUD", "VIE", "2026-08-22", 30, dep="08:00", arr="09:30"),
        ("VIE", "LIS", "2026-08-22"): _df("VIE", "LIS", "2026-08-22", 40, dep="13:00", arr="16:00"),
        ("LIS", "VIE", "2026-08-26"): _df("LIS", "VIE", "2026-08-26", 45, dep="10:00", arr="13:00"),
        ("VIE", "BUD", "2026-08-26"): _df("VIE", "BUD", "2026-08-26", 25, dep="17:00", arr="18:30"),
        ("LIS", "VIE", "2026-08-28"): _df("LIS", "VIE", "2026-08-28", 50, dep="10:00", arr="13:00"),
        ("VIE", "BUD", "2026-08-28"): _df("VIE", "BUD", "2026-08-28", 28, dep="17:00", arr="18:30"),
    }
    return anywhere, exact, cal


def _run(spec, planner):
    plan = compile_plan(spec, planner.registry)
    return planner.execute(plan, spec)["results"]


def test_verified_s5_surfaces_with_buffer_in_total_and_disclosure():
    anywhere, exact, cal = _good_tables()
    p = _planner(anywhere, exact, cal, via=["VIE"])
    results = _run(_spec(via=["VIE"]), p)
    s5 = [d for d in results if d["shape"] == "S5"]
    assert len(s5) == 1
    d = s5[0]
    assert d["origin"] == "BUD" and d["destination"] == "LIS"
    assert d["return_date"] == "2026-08-26"  # cheapest valid return from the sweep
    # 30+40+45+25 = 140 fares + 25 buffer = 165
    assert d["price_eur"] == 165.0
    assert d["price_confidence"] == "exact"
    conn = d["connection"]
    assert conn == {"hub": "VIE", "connect_out_minutes": 210, "connect_ret_minutes": 240,
                    "verified": True, "separate_tickets": True, "buffer_eur": 25.0}
    # legs are the four flight segments through the hub, chronological.
    assert [(l["origin"], l["destination"]) for l in d["legs"]] == [
        ("BUD", "VIE"), ("VIE", "LIS"), ("LIS", "VIE"), ("VIE", "BUD")]
    # disclosure ALWAYS present in why (separate tickets + buffer).
    assert "SEPARATE tickets" in d["why"] and "self-transfer buffer" in d["why"]
    # no misleading single combined booking link.
    assert d["links"] == {}
    # return-window sweep budget: 2 CAL/direction (1 month) + 2 exact return legs
    # for the one shortlisted candidate; the outbound is reused from discovery.
    assert len(p.ryanair.exact_calls) == 2
    assert sorted(p.ryanair.cal_calls) == [
        ("LIS", "VIE", "2026-08"), ("VIE", "BUD", "2026-08")]


def test_s5_deal_id_golden_vector():
    # Frozen S5 golden (CONTRACT §5 changelog 2026-07-12).
    assert deal_id("BUD", "LIS", "2026-08-22", "2026-08-26", "S5", ["ryanair"]) == "ace1d456ef"
    anywhere, exact, cal = _good_tables()
    p = _planner(anywhere, exact, cal, via=["VIE"])
    d = [x for x in _run(_spec(via=["VIE"]), p) if x["shape"] == "S5"][0]
    assert d["deal_id"] == "ace1d456ef"


def test_unverified_never_shown_when_a_leg_is_unbookable():
    anywhere, exact, cal = _good_tables()
    # Both candidate return dates lose leg4 -> no date verifies -> dropped.
    del exact[("VIE", "BUD", "2026-08-26")]
    del exact[("VIE", "BUD", "2026-08-28")]
    p = _planner(anywhere, exact, cal, via=["VIE"])
    results = _run(_spec(via=["VIE"]), p)
    assert [d for d in results if d["shape"] == "S5"] == []  # dropped, never shown


def test_unverified_never_shown_when_verified_times_fail_mct():
    anywhere, exact, cal = _good_tables()
    # Both swept return dates' connections become 17:00 depart vs 16:55 arrival
    # = 5 min -> below MCT, so neither the cheapest nor the retry verifies.
    exact[("LIS", "VIE", "2026-08-26")] = _df("LIS", "VIE", "2026-08-26", 45,
                                              dep="10:00", arr="16:55")
    exact[("LIS", "VIE", "2026-08-28")] = _df("LIS", "VIE", "2026-08-28", 50,
                                              dep="10:00", arr="16:55")
    p = _planner(anywhere, exact, cal, via=["VIE"])
    results = _run(_spec(via=["VIE"]), p)
    assert [d for d in results if d["shape"] == "S5"] == []


def test_via_none_disables_hub_fanout():
    spec = _spec(via="none")
    assert resolve_hubs(spec, Planner().registry, "BUD") == []
    plan = compile_plan(spec, Planner().registry)
    assert [c for c in plan.calls if c.shape == "S5"] == []
    assert plan.via_hub_verify_max == 0


def test_via_auto_lists_reachable_hubs():
    spec = _spec()  # default via=auto
    hubs = resolve_hubs(spec, Planner().registry, "BUD")
    assert "VIE" in hubs and "BCN" in hubs
    assert "BUD" not in hubs


def test_via_explicit_list_restricts_to_named_hubs():
    spec = _spec(via=["VIE", "BGY"])
    assert resolve_hubs(spec, Planner().registry, "BUD") == ["BGY", "VIE"]


def test_via_hub_one_way_refused():
    from flight_deals.engine.planner import PlannerRefusal
    spec = parse_spec({"origins": ["BUD"], "where": "portugal", "depart": "2026-08-22",
                       "shapes": ["via-hub"], "carriers": ["ryanair"]})
    with pytest.raises(PlannerRefusal):
        compile_plan(spec, Planner().registry)


def test_plan_reserves_verify_calls_in_estimate():
    spec = _spec(via=["VIE"])  # depart 08-22..08-24, nights 4-7 -> 1 return month
    plan = compile_plan(spec, Planner().registry)
    concrete = len(plan.calls)
    # Return-window sweep reserves per candidate: 2 CAL (1 month) + 2 exact + 2
    # retry = 6; shortlist 6 -> 36 reserved beyond the concrete descriptors.
    assert plan.estimated_calls == concrete + 36
    assert plan.via_hub_hubs == ["VIE"]
    assert plan.to_dict()["via_hub"] == {"hubs": ["VIE"], "shortlist": 6, "verify_calls_max": 36}


# --------------------------------------------------------------------------- #
# PURE: return-window date selection (Task 17)                                 #
# --------------------------------------------------------------------------- #
def test_select_return_dates_picks_cheapest_valid_alignment():
    # nights 4-7 off out 08-22 -> return window 08-26..08-29.
    dh = [_cal("LIS", "VIE", "2026-08-26", 45), _cal("LIS", "VIE", "2026-08-27", 20),
          _cal("LIS", "VIE", "2026-08-28", 50)]
    hb = [_cal("VIE", "BUD", "2026-08-26", 25), _cal("VIE", "BUD", "2026-08-27", 90),
          _cal("VIE", "BUD", "2026-08-28", 28)]
    opts = vh.select_return_dates("2026-08-22", dh, hb, nights_lo=4, nights_hi=7)
    # 08-26=70, 08-27=110, 08-28=78 -> cheapest first.
    assert [(o.ret_date, o.ret_price_eur) for o in opts] == [
        ("2026-08-26", 70.0), ("2026-08-28", 78.0), ("2026-08-27", 110.0)]


def test_select_return_dates_filters_outside_nights_window():
    # 08-25 is out+3 (below min 4 nights); 08-30 is out+8 (above max 7) -> excluded.
    dh = [_cal("LIS", "VIE", "2026-08-25", 5), _cal("LIS", "VIE", "2026-08-26", 45),
          _cal("LIS", "VIE", "2026-08-30", 5)]
    hb = [_cal("VIE", "BUD", "2026-08-25", 5), _cal("VIE", "BUD", "2026-08-26", 25),
          _cal("VIE", "BUD", "2026-08-30", 5)]
    opts = vh.select_return_dates("2026-08-22", dh, hb, nights_lo=4, nights_hi=7)
    assert [o.ret_date for o in opts] == ["2026-08-26"]


def test_select_return_dates_requires_both_legs_on_the_date():
    # 08-27 has only leg3 (no leg4) -> not a valid alignment.
    dh = [_cal("LIS", "VIE", "2026-08-26", 45), _cal("LIS", "VIE", "2026-08-27", 10)]
    hb = [_cal("VIE", "BUD", "2026-08-26", 25)]
    opts = vh.select_return_dates("2026-08-22", dh, hb, nights_lo=4, nights_hi=7)
    assert [o.ret_date for o in opts] == ["2026-08-26"]


# --------------------------------------------------------------------------- #
# Sweep orchestration: retry on next-best date, exact replaces CAL, multi-month#
# --------------------------------------------------------------------------- #
def test_mct_fail_on_cheapest_retries_next_best_date_and_succeeds():
    anywhere, exact, cal = _good_tables()
    # Cheapest return (08-26) fails MCT; the next-best (08-28) verifies.
    exact[("LIS", "VIE", "2026-08-26")] = _df("LIS", "VIE", "2026-08-26", 45,
                                              dep="10:00", arr="16:55")  # 5 min gap
    p = _planner(anywhere, exact, cal, via=["VIE"])
    d = [x for x in _run(_spec(via=["VIE"]), p) if x["shape"] == "S5"][0]
    assert d["return_date"] == "2026-08-28"
    # 30+40 outbound + 50+28 return exact + 25 buffer = 173.
    assert d["price_eur"] == 173.0
    # 4 exact calls: 2 on the failed cheapest date + 2 on the retry date.
    assert len(p.ryanair.exact_calls) == 4


def test_exact_prices_replace_cal_selection_in_total():
    anywhere, exact, cal = _good_tables()
    # CAL advertises leg3 at 45 but the EXACT fare on 08-26 is 60 -> the total
    # must use the exact 60, never the 45 selection minimum (no estimate leak).
    exact[("LIS", "VIE", "2026-08-26")] = _df("LIS", "VIE", "2026-08-26", 60,
                                              dep="10:00", arr="13:00")
    p = _planner(anywhere, exact, cal, via=["VIE"])
    d = [x for x in _run(_spec(via=["VIE"]), p) if x["shape"] == "S5"][0]
    # 30+40 + 60(exact leg3) + 25(exact leg4) + 25 buffer = 180, NOT 165.
    assert d["price_eur"] == 180.0
    assert d["price_confidence"] == "exact"


def test_multi_month_return_window_queries_two_months_and_selects_across_them():
    # Outbound 08-27, nights 4-7 -> return window 08-31..09-03 (spans Aug + Sept).
    anywhere = {
        "BUD": [_df("BUD", "VIE", "2026-08-27", 30, dep="08:00", arr="09:30")],
        "VIE": [_df("VIE", "LIS", "2026-08-27", 40, dep="13:00", arr="16:00")],
    }
    cal = {
        ("LIS", "VIE", "2026-08"): [_cal("LIS", "VIE", "2026-08-31", 45)],
        ("VIE", "BUD", "2026-08"): [_cal("VIE", "BUD", "2026-08-31", 25)],
        ("LIS", "VIE", "2026-09"): [_cal("LIS", "VIE", "2026-09-02", 100)],
        ("VIE", "BUD", "2026-09"): [_cal("VIE", "BUD", "2026-09-02", 100)],
    }
    exact = {
        ("LIS", "VIE", "2026-08-31"): _df("LIS", "VIE", "2026-08-31", 45, dep="10:00", arr="13:00"),
        ("VIE", "BUD", "2026-08-31"): _df("VIE", "BUD", "2026-08-31", 25, dep="17:00", arr="18:30"),
    }
    spec = parse_spec({"origins": ["BUD"], "where": "portugal | spain",
                       "depart": "2026-08-27..2026-08-29", "nights": "4-7",
                       "shapes": ["via-hub"], "carriers": ["ryanair"], "via": ["VIE"]})
    p = _planner(anywhere, exact, cal, via=["VIE"])
    d = [x for x in _run(spec, p) if x["shape"] == "S5"][0]
    assert d["return_date"] == "2026-08-31"  # cheapest across both months
    # both the Aug and Sept calendars are queried for each return direction.
    months = {m for _o, _d, m in p.ryanair.cal_calls}
    assert months == {"2026-08", "2026-09"}


# --------------------------------------------------------------------------- #
# alert fires on a verified S5 total; gems never extend S5; check declines S5  #
# --------------------------------------------------------------------------- #
def test_verified_s5_can_alert_on_buffer_inclusive_total(tmp_path):
    from flight_deals.state.alert_state import AlertMachine
    from flight_deals import output

    deal = output.build_deal(
        shape="S5", origin="BUD", destination="LIS", out_date="2026-08-22",
        return_date="2026-08-26", price_eur=165.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[output.flight_leg("BUD", "VIE", "ryanair", "2026-08-22", 30.0)],
        connection=output.connection_summary("VIE", 210, 240, buffer_eur=25.0),
        why="x",
    )
    m = AlertMachine(path=tmp_path / "alert_state.json")
    # buffer-inclusive total (165) crosses a 180 threshold -> fires.
    assert m.evaluate(search_name="w", deal=deal, max_price=180.0) is True
    # a lower-buffer... an approximate S5 would never fire (double-guard), but a
    # verified S5 is exact, so the first crossing fires exactly once.
    assert m.evaluate(search_name="w", deal=deal, max_price=180.0) is False


def test_gems_never_extend_s5():
    from flight_deals.engine import gems as gems_engine
    assert "S5" not in gems_engine.EXTENDABLE_SHAPES
    s5_deal = {"shape": "S5", "origin": "BUD", "destination": "LIS", "out_date": "2026-08-22",
               "return_date": "2026-08-26", "price_eur": 165.0, "price_confidence": "exact",
               "carriers": ["ryanair"], "deal_id": "ace1d456ef"}
    # Even handed a gem whose gateway is LIS, an S5 deal is not extended.
    from flight_deals.models import Gem, GemGateway, GemLeg
    gem = Gem(slug="x", name="X", country="Portugal", tags=["island"],
              gateways=[GemGateway(airport="LIS", legs=[GemLeg(mode="ferry", from_place="a",
                        to_place="b", minutes=60, cost_eur=10)], total_minutes=60,
                        total_cost_eur=10)])
    variants = gems_engine.extend_deals([s5_deal], [gem], window=("2026-08-22", "2026-08-24"),
                                        forced=True)
    assert variants == []


def test_check_declines_s5_composite():
    from flight_deals.engine import intents
    from flight_deals.state import snapshots

    snap = {"deal_id": "ace1d456ef", "origin": "BUD", "destination": "LIS",
            "out_date": "2026-08-22", "return_date": "2026-08-26", "shape": "S5",
            "carriers": ["ryanair"], "price_eur": 165.0, "seen_at": "2026-07-12T00:00:00+00:00"}

    def fake_latest(_id):
        return snap

    orig = snapshots.latest
    snapshots.latest = fake_latest
    try:
        env, code = intents.check_deal("ace1d456ef", today=date(2026, 7, 12))
    finally:
        snapshots.latest = orig
    assert code == 0
    assert env["results"] == []
    assert "S5 composite" in env["summary"]
