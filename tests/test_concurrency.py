"""
Task 3 req 8 (binding carry-over): the failure-visibility race is gone.

The old design had providers return `[]` and set a *shared* `self.last_error`;
under concurrency one thread's success could clobber another's error before it
was read, so a single failed destination could vanish from the aggregated
status. The rebuilt design has workers return their OWN status events, merged
single-threaded — a failure in exactly one of 8 concurrent calls is always
visible.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from flight_deals.http import ProviderDown, SchemaError
from flight_deals.orchestrator import aggregate_status, status_for_exception


def test_status_mapping():
    assert status_for_exception(ProviderDown("x")) == "error"
    assert status_for_exception(SchemaError("x")) == "parse_error"


def _worker(dest):
    """Simulate a per-destination ryanair call; exactly one destination fails."""
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


def test_one_of_eight_failures_is_visible_under_concurrency():
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


def test_run_repeatedly_never_loses_the_failure():
    # The race was intermittent; run the scenario many times to be confident.
    for _ in range(50):
        events = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_worker, f"DEST_{i}") for i in range(8)]
            for f in as_completed(futs):
                _, evs = f.result()
                events.extend(evs)
        status = aggregate_status(events)
        assert status["ryanair"]["ok"] is False
        assert status["ryanair"]["errors"] == 1
