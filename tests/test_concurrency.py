"""
Task 3 req 8 (binding carry-over): the failure-visibility race is gone.

The old design had providers return `[]` and set a *shared* `self.last_error`;
under concurrency one thread's success could clobber another's error before it
was read, so a single failed destination could vanish from the aggregated
status. The rebuilt design has workers return their OWN status events, merged
single-threaded — a failure in exactly one of 8 concurrent calls is always
visible.

The real race now lives in `Planner.execute` fanning out one Wizz timetable
call per destination across the shared worker pool, all driving a *single
shared* `planner.wizz` instance — so the regression test below drives that real
path (real `Planner`, the real process-wide `ThreadPoolExecutor`, one shared
`planner.wizz` instance) rather than a from-scratch simulation that never
touches production code. (The legacy `DealOrchestrator.search_by_category` path
these tests used to drive was deleted; this is the equivalent-rigor port.)
"""

import random
from concurrent.futures import ThreadPoolExecutor, as_completed

from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.http import ProviderDown, SchemaError
from flight_deals.models import DayFare
from flight_deals.orchestrator import aggregate_status, status_for_exception


def test_status_mapping():
    assert status_for_exception(ProviderDown("x")) == "error"
    assert status_for_exception(SchemaError("x")) == "parse_error"


def _worker(dest):
    """Fabricated per-destination event, merged the same way the real
    aggregation does. Exercises `aggregate_status` semantics only — NOT the
    real concurrency race (see `test_one_of_eight_failures_is_visible_under_concurrency`
    below for that)."""
    events = []
    try:
        if dest == "DEST_3":
            raise ProviderDown("simulated outage on one route")
        deals = [dest]
        events.append({"provider": "ryanair", "status": "ok"})
    except Exception as e:
        deals = []
        events.append({"provider": "ryanair", "status": status_for_exception(e), "detail": str(e)})
    return deals, events


def test_aggregate_status_merges_events():
    dests = [f"DEST_{i}" for i in range(8)]
    all_events = []
    all_deals = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_worker, d): d for d in dests}
        for f in as_completed(futs):
            deals, events = f.result()
            all_deals.extend(deals)
            all_events.extend(events)

    status = aggregate_status(all_events)
    # 7 succeeded, 1 failed
    assert len(all_deals) == 7
    assert "ryanair" in status
    assert status["ryanair"]["ok"] is False          # the single failure is NOT lost
    assert status["ryanair"]["status"] == "error"
    assert status["ryanair"]["errors"] == 1
    assert status["ryanair"]["calls"] == 8
    assert status["ryanair"]["last_error"]


# --------------------------------------------------------------------------- #
# Real production path: a real Planner, the real process-wide worker pool, and #
# the SAME shared `planner.wizz` instance every per-destination call goes to.  #
# --------------------------------------------------------------------------- #
_DESTS = ["CFU", "CHQ", "EFL", "HER", "JMK", "JTR", "KGS", "RHO"]  # 8 registry airports
_FAILING = "HER"


def _spec():
    # One-way (no nights) Wizz-only sweep over exactly the 8 destinations above:
    # compile_plan emits one timetable call per destination -> 8 concurrent calls
    # on the shared planner.wizz instance.
    return parse_spec({
        "origins": ["BUD"], "destinations": list(_DESTS),
        "depart": "2026-08-01..2026-08-03", "carriers": ["wizzair"],
    })


def _flaky_timetable(origin, dest, date_from, date_to, use_cache=True):
    """Stands in for the shared `planner.wizz.timetable`. Exactly one destination
    raises a typed provider exception; the other 7 return one cheap in-window
    outbound fare. A tiny random sleep jitter encourages thread interleaving so
    the historical race (a shared mutable `last_error` clobbered across threads)
    would actually manifest if it were still present."""
    import time as _time
    _time.sleep(random.uniform(0, 0.005))
    if dest == _FAILING:
        raise ProviderDown(f"simulated outage on {dest}")
    out = DayFare(origin=origin, destination=dest, date="2026-08-01", price_eur=42.0,
                  currency_original="EUR", price_confidence="approximate",
                  carrier="wizzair", source_endpoint="wizz/timetable")
    return ([out], [])


def _run_once():
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda *a, **k: []
    planner.ryanair.oneway_fares = lambda *a, **k: []
    # SAME shared instance every thread-pool worker calls into.
    planner.wizz.timetable = _flaky_timetable
    spec = _spec()
    plan = compile_plan(spec, planner.registry)
    return planner.execute(plan, spec)


def test_one_of_eight_failures_is_visible_under_concurrency():
    outcome = _run_once()
    # The single failure is NOT lost: it surfaces in the frozen sources map.
    assert outcome["sources"]["wizzair"] == "error"
    # The 7 successful destinations' deals are still returned.
    got = {d["destination"] for d in outcome["results"]}
    assert len(got) == 7
    assert _FAILING not in got


def test_run_repeatedly_never_loses_the_failure():
    # The race was intermittent; run the real planner path many times to be
    # confident it never regresses.
    for _ in range(50):
        outcome = _run_once()
        assert outcome["sources"]["wizzair"] == "error"
        got = {d["destination"] for d in outcome["results"]}
        assert len(got) == 7
        assert _FAILING not in got
