"""planner.py — compile refusals, TT window-clip + pairing, cross-carrier merge,
budget/route_status, and the session-lifecycle regression."""

import threading

import pytest

from flight_deals import http
from flight_deals.engine.planner import (
    DEFAULT_MAX_CALLS,
    Planner,
    PlannerRefusal,
    _pair_timetable,
    check_max_calls,
    compile_plan,
)
from flight_deals.engine.spec import parse_spec
from flight_deals.models import DayFare, FareLeg, FarePair


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
def test_compile_refuses_non_direct_shape():
    with pytest.raises(PlannerRefusal) as ei:
        compile_plan(_spec(shapes=["via-hub"]))
    assert "not yet enabled" in ei.value.hint


def test_compile_refuses_one_way():
    with pytest.raises(PlannerRefusal) as ei:
        compile_plan(parse_spec({"where": "seaside", "depart": "2026-08"}))
    assert "nights" in ei.value.hint


def test_compile_is_deterministic_and_sorted():
    p1 = compile_plan(_spec()).to_dict()
    p2 = compile_plan(_spec()).to_dict()
    assert p1 == p2
    assert p1["calls"][0]["mode"] == "anywhere"  # anywhere first


def test_compile_respects_carriers_filter():
    only_ry = compile_plan(_spec(carriers=["ryanair"]))
    assert all(c.provider == "ryanair" for c in only_ry.calls)
    assert only_ry.estimated_calls == 1  # just RT-ANYWHERE, no TT


def test_check_max_calls_refuses_with_narrow_hint():
    plan = compile_plan(parse_spec({"where": "seaside", "depart": "2026-08-22..2026-08-24", "nights": "5-8"}))
    assert plan.estimated_calls > DEFAULT_MAX_CALLS
    with pytest.raises(PlannerRefusal) as ei:
        check_max_calls(plan, DEFAULT_MAX_CALLS)
    assert "--max-calls" in ei.value.hint


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
    for _ in range(6):
        pl.execute(compile_plan(spec), spec)

    assert http.get_executor(pl.config.max_workers) is first_executor  # reused, not per-search
    assert http.session_count() <= pl.config.max_workers  # bounded, not 6*N
    # No unbounded thread growth either.
    assert threading.active_count() <= threads_before + pl.config.max_workers
