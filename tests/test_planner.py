"""planner.py — compile refusals, TT window-clip + pairing, cross-carrier merge,
budget/route_status, and the session-lifecycle regression."""

import threading

import pytest
import responses

from flight_deals import http
from flight_deals.engine.planner import (
    DEFAULT_MAX_CALLS,
    Planner,
    PlannerRefusal,
    _pair_timetable,
    check_max_calls,
    check_where_gate,
    compile_plan,
)
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare, FareLeg, FarePair
from flight_deals.registry.destinations import DestinationRegistry


def _spec(**over):
    base = {"origins": ["BUD"], "where": "croatia & seaside",
            "depart": "2026-08-22..2026-08-24", "nights": "5-8"}
    base.update(over)
    return parse_spec(base)


def _dayfare(o, d, date_, price, carrier="wizzair", conf="approximate"):
    return DayFare(origin=o, destination=d, date=date_, price_eur=price,
                   currency_original="EUR", price_confidence=conf, carrier=carrier,
                   source_endpoint="wizz/timetable")


def _farepair(dest, out_date, ret_date, total, carrier="ryanair", conf="exact"):
    n = 5
    return FarePair(
        origin="BUD", destination=dest, out_date=out_date, return_date=ret_date, nights=n,
        total_price_eur=total, currency_original="EUR", price_confidence=conf, carrier=carrier,
        source_endpoint="farfnd/roundTripFares",
        outbound=FareLeg(origin="BUD", destination=dest, date=out_date, price_eur=total / 2, carrier=carrier),
        inbound=FareLeg(origin=dest, destination="BUD", date=ret_date, price_eur=total / 2, carrier=carrier),
    )


# --- compile refusals ------------------------------------------------------ #
def test_compile_refuses_via_hub_shape():
    """via-hub (S5) is the only shape still refused (Task 10 enabled S3/S4)."""
    with pytest.raises(PlannerRefusal) as ei:
        compile_plan(_spec(shapes=["via-hub"]))
    assert "via-hub" in ei.value.hint
    assert "not enabled" in ei.value.hint


def test_compile_accepts_extended_origin_and_open_jaw():
    """extended-origin (S3) and open-jaw (S4) compile without a refusal."""
    plan = compile_plan(_spec(shapes=["direct", "extended-origin", "open-jaw"]))
    assert any(c.shape == "S3" for c in plan.calls)
    assert any(c.shape == "S4" for c in plan.calls)


def test_compile_one_way_uses_oneway_anywhere():
    """One-way (no nights) is enabled in Task 7: it compiles to an OW-ANYWHERE
    Ryanair call (S1) plus Wizz TT per matched dest — never refused."""
    plan = compile_plan(parse_spec({"where": "seaside", "depart": "2026-08-22..2026-08-24"}))
    ow = [c for c in plan.calls if c.provider == "ryanair"]
    assert len(ow) == 1
    assert ow[0].endpoint == "oneWayFares"
    assert ow[0].shape == "S1"
    assert all(c.shape == "S1" for c in plan.calls)


def test_compile_is_deterministic_and_sorted():
    p1 = compile_plan(_spec()).to_dict()
    p2 = compile_plan(_spec()).to_dict()
    assert p1 == p2
    assert p1["calls"][0]["mode"] == "anywhere"  # anywhere first


def test_compile_respects_carriers_filter():
    only_ry = compile_plan(_spec(carriers=["ryanair"]))
    assert all(c.provider == "ryanair" for c in only_ry.calls)
    assert only_ry.estimated_calls == 1  # just RT-ANYWHERE, no TT


def test_check_max_calls_refuses_with_single_exact_command_hint():
    """Review item: the old hint offered 3 options ("drop a shape ..., narrow
    the search ..., or raise the cap ...") and the skill's own worked example
    (--where "seaside | italy | spain") tripped it. The hint must now be ONE
    exact corrected command (--max-calls raised to the estimate rounded up to
    the next 5) plus a single trailing "or narrow --where" clause — no menu."""
    plan = compile_plan(parse_spec({"where": "seaside", "depart": "2026-08-22..2026-08-24", "nights": "5-8"}))
    assert plan.estimated_calls > DEFAULT_MAX_CALLS
    with pytest.raises(PlannerRefusal) as ei:
        check_max_calls(plan, DEFAULT_MAX_CALLS)
    hint = ei.value.hint
    assert "--max-calls" in hint
    rounded = ((plan.estimated_calls + 4) // 5) * 5
    assert f"--max-calls {rounded}" in hint
    assert "or narrow --where" in hint
    # single-option shape: the old multi-option prose must be gone.
    assert "drop a shape" not in hint
    assert "raise the cap" not in hint


def test_check_max_calls_rounds_estimate_up_to_next_5():
    class _Plan:
        estimated_calls = 41

    with pytest.raises(PlannerRefusal) as ei:
        check_max_calls(_Plan(), 40)
    assert "--max-calls 45" in ei.value.hint  # 41 -> rounded up to 45


def test_check_max_calls_estimate_already_multiple_of_5_stays_same():
    class _Plan:
        estimated_calls = 50

    with pytest.raises(PlannerRefusal) as ei:
        check_max_calls(_Plan(), 40)
    assert "--max-calls 50" in ei.value.hint


# --- TT window-clip + pairing ---------------------------------------------- #
def test_pair_timetable_clips_and_pairs_cheapest():
    spec = _spec()
    # Rows come back UN-clipped (Task 4 note): include out-of-window dates.
    outs = [
        _dayfare("BUD", "ZAD", "2026-08-20", 20.0),  # before window -> clipped
        _dayfare("BUD", "ZAD", "2026-08-23", 30.0),  # in window
        _dayfare("BUD", "ZAD", "2026-08-24", 25.0),  # in window (cheaper)
    ]
    rets = [
        _dayfare("ZAD", "BUD", "2026-08-28", 40.0),  # 5 nights from 08-23
        _dayfare("ZAD", "BUD", "2026-09-20", 10.0),  # too far -> clipped
    ]
    cand = _pair_timetable("BUD", "ZAD", outs, rets, spec.depart_spec, spec.nights_range)
    assert cand is not None
    assert cand.out_date == "2026-08-23"  # 08-24+? -> but 08-23->08-28 = 5 nights valid & cheapest total
    assert cand.return_date == "2026-08-28"
    assert cand.price_eur == 70.0
    assert cand.price_confidence == "approximate"


def test_pair_timetable_no_valid_pair_returns_none():
    spec = _spec(nights="5-8")
    outs = [_dayfare("BUD", "ZAD", "2026-08-23", 30.0)]
    rets = [_dayfare("ZAD", "BUD", "2026-08-25", 40.0)]  # only 2 nights -> outside 5-8
    assert _pair_timetable("BUD", "ZAD", outs, rets, spec.depart_spec, spec.nights_range) is None


def test_pair_timetable_month_kind_is_unaffected_regression():
    """Regression: a ``month`` depart (no explicit dates list) must keep
    considering every in-window outbound date, exactly like ``window`` above."""
    spec = _spec(depart="2026-08")
    outs = [
        _dayfare("BUD", "ZAD", "2026-08-05", 30.0),
        _dayfare("BUD", "ZAD", "2026-08-12", 20.0),  # cheapest, arbitrary date, must still win
    ]
    rets = [
        _dayfare("ZAD", "BUD", "2026-08-10", 40.0),  # 5 nights from 08-05
        _dayfare("ZAD", "BUD", "2026-08-17", 10.0),  # 5 nights from 08-12
    ]
    cand = _pair_timetable("BUD", "ZAD", outs, rets, spec.depart_spec, spec.nights_range)
    assert cand.out_date == "2026-08-12" and cand.return_date == "2026-08-17"
    assert cand.price_eur == 30.0


# --- date-list ("dates") kind must not silently widen (quality review fix) - #
def test_pair_timetable_dates_kind_only_pairs_listed_outbound_dates():
    """depart="2026-08-01,2026-08-15,2026-08-29" must only ever pair an
    outbound on one of those THREE dates — never on some other in-window date
    (2026-08-10 below), even when that other date is far cheaper."""
    spec = _spec(depart="2026-08-01,2026-08-15,2026-08-29")
    assert spec.depart_spec.kind == "dates"
    outs = [
        _dayfare("BUD", "ZAD", "2026-08-01", 50.0),   # listed
        _dayfare("BUD", "ZAD", "2026-08-10", 1.0),    # NOT listed - must be excluded
        _dayfare("BUD", "ZAD", "2026-08-15", 40.0),   # listed
    ]
    rets = [
        _dayfare("ZAD", "BUD", "2026-08-08", 20.0),   # pairs w/ 08-01 (7 nights) -> 70
        _dayfare("ZAD", "BUD", "2026-08-16", 1.0),    # pairs w/ 08-10 (6 nights) -> 2 if not excluded
        _dayfare("ZAD", "BUD", "2026-08-21", 10.0),   # pairs w/ 08-15 (6 nights) -> 50
    ]
    cand = _pair_timetable("BUD", "ZAD", outs, rets, spec.depart_spec, spec.nights_range)
    assert cand is not None
    assert cand.out_date != "2026-08-10"
    assert cand.out_date == "2026-08-15" and cand.return_date == "2026-08-21"
    assert cand.price_eur == 50.0


def test_farepair_excludes_out_date_not_in_dates_list():
    """Confirmed repro from quality review: depart="2026-08-01,2026-08-29"
    must NOT accept a FarePair outbound on 2026-08-15 (the un-listed midpoint
    of the request window) — only exactly-listed outbound dates may surface."""
    spec = _spec(depart="2026-08-01,2026-08-29", where="sicily")
    ry = [
        _farepair("CTA", "2026-08-15", "2026-08-20", 10.0),  # NOT listed - must be excluded
        _farepair("CTA", "2026-08-01", "2026-08-08", 70.0),  # listed - kept
    ]
    out = _planner_with(ry, {}).execute(compile_plan(spec), spec)
    cta = [d for d in out["results"] if d["destination"] == "CTA"]
    assert len(cta) == 1
    assert cta[0]["out_date"] == "2026-08-01"


def test_compile_dates_kind_plan_reflects_request_window_not_exact_list():
    """compile stays pure/window-shaped even for a dates-kind depart: the
    plan's params carry the request window (out_from/out_to spanning the
    listed dates) — it is execute() that filters back down to exactly the
    listed dates (see tests above)."""
    spec = _spec(depart="2026-08-01,2026-08-29", where="sicily")
    assert spec.depart_spec.kind == "dates"
    plan = compile_plan(spec)
    anywhere = [c for c in plan.calls if c.mode == "anywhere"][0]
    assert anywhere.params["out_from"] == "2026-08-01"
    assert anywhere.params["out_to"] == "2026-08-29"


# --- cross-carrier merge (exact beats approximate on tie) ------------------ #
def _planner_with(ry_pairs, wizz_map):
    pl = Planner()
    pl.ryanair.roundtrip_fares = lambda *a, **k: list(ry_pairs)
    pl.wizz.timetable = lambda origin, dest, *a, **k: wizz_map.get(dest, ([], []))
    return pl


def test_merge_exact_beats_approximate_on_tie():
    spec = _spec(where="sicily")  # CTA, PMO
    ry = [_farepair("CTA", "2026-08-23", "2026-08-28", 60.0)]
    wizz = {"CTA": ([_dayfare("BUD", "CTA", "2026-08-23", 30.0)],
                    [_dayfare("CTA", "BUD", "2026-08-28", 30.0)])}  # 60.0 approximate, tie
    out = _planner_with(ry, wizz).execute(compile_plan(spec), spec)
    cta = [d for d in out["results"] if d["destination"] == "CTA"]
    assert len(cta) == 1 and cta[0]["price_confidence"] == "exact"


def test_merge_keeps_cheaper_wizz_when_actually_cheaper():
    spec = _spec(where="sicily")
    ry = [_farepair("CTA", "2026-08-23", "2026-08-28", 60.0)]
    wizz = {"CTA": ([_dayfare("BUD", "CTA", "2026-08-23", 20.0)],
                    [_dayfare("CTA", "BUD", "2026-08-28", 20.0)])}  # 40.0 approximate, cheaper
    out = _planner_with(ry, wizz).execute(compile_plan(spec), spec)
    cta = [d for d in out["results"] if d["destination"] == "CTA"][0]
    assert cta["price_eur"] == 40.0 and cta["price_confidence"] == "approximate"


# --- budget + route_status ------------------------------------------------- #
def test_budget_filter_yields_no_match_when_all_priced_out():
    spec = _spec(where="sicily", budget=1)
    ry = [_farepair("CTA", "2026-08-23", "2026-08-28", 60.0)]
    out = _planner_with(ry, {}).execute(compile_plan(spec), spec)
    assert out["results"] == []
    assert out["route_status"] == "no_match"
    assert out["exit_code"] == 0


def test_empty_no_failure_is_no_service():
    spec = _spec(where="sicily")
    out = _planner_with([], {}).execute(compile_plan(spec), spec)
    assert out["results"] == [] and out["route_status"] == "no_service" and out["exit_code"] == 0


def test_provider_failure_on_empty_is_provider_error_exit_1():
    from flight_deals.http import ProviderDown

    spec = _spec(where="sicily")
    pl = Planner()
    def boom(*a, **k):
        raise ProviderDown("down")
    pl.ryanair.roundtrip_fares = boom
    pl.wizz.timetable = lambda *a, **k: ([], [])
    out = pl.execute(compile_plan(spec), spec)
    assert out["results"] == []
    assert out["route_status"] == "provider_error"
    assert out["exit_code"] == 1

    # CONTRACT §3: exit 1 MUST carry error+hint on the envelope `run()` builds
    # (execute()'s raw outcome dict above has no error/hint of its own — those
    # are attached at the envelope layer, asserted here).
    env, exit_code = pl.run(spec)
    assert exit_code == 1
    assert env["results"] == []
    assert env["route_status"] == "provider_error"
    assert env["error"] == "provider_error"
    assert env["hint"]


# --- session-lifecycle regression (Task 3 carry-over, binding) ------------- #
def test_execute_reuses_pool_and_does_not_leak_sessions():
    """Repeated execute() in one process must not grow per-thread sessions:
    the shared pool is reused, so worker threads (and their sessions) are
    bounded by max_workers, not by the number of searches."""
    spec = _spec(where="sicily")

    # Force each worker thread to lazily create its http session, as a real
    # provider call would, so the count is meaningful.
    pl = Planner()
    def touch_and_return(*a, **k):
        http._session()
        return []
    pl.ryanair.roundtrip_fares = touch_and_return
    pl.wizz.timetable = lambda *a, **k: (http._session(), ([], []))[1]

    first_executor = http.get_executor(pl.config.max_workers)
    threads_before = threading.active_count()
    sessions_before = http.session_count()
    for _ in range(6):
        pl.execute(compile_plan(spec), spec)

    assert http.get_executor(pl.config.max_workers) is first_executor  # reused, not per-search
    # Sessions created by THIS repeated execution must be bounded by the shared
    # pool's own worker count, not 6x (one set per search). Measured as growth
    # from this test's own baseline — not an absolute count — because
    # `http.session_count()` is a process-wide registry shared with unrelated
    # tests (e.g. test_http.py's own per-thread test creates sessions on
    # threads outside this pool); an absolute bound would make this
    # assertion depend on suite-wide test order/timing instead of on the
    # invariant this test actually exercises.
    assert http.session_count() - sessions_before <= pl.config.max_workers
    # No unbounded thread growth either.
    assert threading.active_count() <= threads_before + pl.config.max_workers


# --------------------------------------------------------------------------- #
# check_where_gate (review item: typo'd/empty --where must never reach a     #
# provider) + its wiring into Planner.run() (the `run` --spec CLI path)      #
# --------------------------------------------------------------------------- #
def test_check_where_gate_unknown_tag_empty_destinations_stops_exit_2():
    spec = _spec(where="seasid & italy")
    gate = check_where_gate(spec, DestinationRegistry())
    assert gate.stop and gate.exit_code == 2
    assert "seaside" in gate.env["hint"]
    assert gate.env["results"] == []


def test_check_where_gate_legit_empty_category_stops_exit_0_no_match():
    spec = _spec(where="ski")
    gate = check_where_gate(spec, DestinationRegistry())
    assert gate.stop and gate.exit_code == 0
    assert gate.env["route_status"] == "no_match"
    assert gate.env["next"] == ["flight-deals where list"]


def test_check_where_gate_partial_unknown_tag_continues_with_hint():
    spec = _spec(where="seasid | italy")
    gate = check_where_gate(spec, DestinationRegistry())
    assert not gate.stop
    assert gate.unknown_tags == ["seasid"]
    assert "seaside" in gate.hint


def test_check_where_gate_no_where_is_a_pure_passthrough():
    spec = _spec(where=None)
    gate = check_where_gate(spec, DestinationRegistry())
    assert not gate.stop and gate.unknown_tags == []


@responses.activate
def test_planner_run_stops_before_network_on_unknown_tag_empty_destinations():
    """The standalone `run --spec` path (Planner.run, not intents.run_search)
    must get the SAME where-gate protection — no network call over a
    --where that can never match."""
    spec = _spec(where="seasid & italy")
    pl = Planner()
    env, code = pl.run(spec)
    assert code == 2
    assert "seaside" in env["hint"]
    assert len(responses.calls) == 0


@responses.activate
def test_planner_run_stops_before_network_on_legit_empty_category():
    spec = _spec(where="ski")
    pl = Planner()
    env, code = pl.run(spec)
    assert code == 0
    assert env["route_status"] == "no_match"
    assert len(responses.calls) == 0
