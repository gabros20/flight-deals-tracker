"""
Task 3 req 8 (binding carry-over): the failure-visibility race is gone.

The old design had providers return `[]` and set a *shared* `self.last_error`;
under concurrency one thread's success could clobber another's error before it
was read, so a single failed destination could vanish from the aggregated
status. The rebuilt design has workers return their OWN status events, merged
single-threaded — a failure in exactly one of 8 concurrent calls is always
visible.

The real race lived in `DealOrchestrator._gather`/`_ryanair_oneway` driving a
*single shared* `RyanairProvider` instance across a thread pool — so the
regression test below drives that real path (real `DealOrchestrator`, real
`ThreadPoolExecutor`, one shared `orch.ryanair` instance) rather than a
from-scratch simulation that never touches production code.
"""

import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from flight_deals.http import ProviderDown, SchemaError
from flight_deals.models import Airport
from flight_deals.orchestrator import DealOrchestrator, aggregate_status, status_for_exception


def test_status_mapping():
    assert status_for_exception(ProviderDown("x")) == "error"
    assert status_for_exception(SchemaError("x")) == "parse_error"


def _worker(dest):
    """Fabricated per-destination event, merged the same way the real
    orchestrator does. Exercises `aggregate_status` semantics only — NOT the
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
# Real production path: a real DealOrchestrator, a real ThreadPoolExecutor,   #
# and the SAME shared `orch.ryanair` instance the workers all call into.      #
# --------------------------------------------------------------------------- #

_FAKE_DESTS = [
    Airport(iata=f"D{i}Z", city=f"City{i}", country="XX", lat=0.0, lon=0.0,
             tags=["european-islands"])
    for i in range(8)
]
_FAILING_IATA = "D3Z"


def _flaky_ryanair(origin, date_from, date_to, destination_airport, use_cache=True):
    """Stands in for the shared `orch.ryanair.get_cheapest_flights`. Exactly one
    destination raises a typed provider exception; the other 7 return a small
    valid deal list. A tiny random sleep jitter encourages thread interleaving
    so the historical race (a shared mutable `last_error` clobbered across
    threads) would actually manifest if it were still present."""
    import time as _time
    _time.sleep(random.uniform(0, 0.005))
    if destination_airport == _FAILING_IATA:
        raise ProviderDown(f"simulated outage on {destination_airport}")
    from flight_deals.models import FlightDeal
    return [
        FlightDeal(
            origin=origin,
            destination=destination_airport,
            departure_date=date_from,
            price=42.0,
            currency="EUR",
            source="ryanair",
        )
    ]


def test_one_of_eight_failures_is_visible_under_concurrency(monkeypatch):
    orch = DealOrchestrator()
    monkeypatch.setattr(orch.registry, "get_reachable", lambda *a, **k: list(_FAKE_DESTS))
    # SAME shared instance the thread-pool workers all call into.
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", _flaky_ryanair)
    monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **k: [])

    results = orch.search_by_category(
        "european-islands", "BUD", "2026-08-01", "2026-08-03", fresh=True
    )

    ryanair_status = orch.provider_status["ryanair"]
    assert ryanair_status["ok"] is False           # the single failure is NOT lost
    assert ryanair_status["status"] == "error"
    assert ryanair_status["errors"] == 1
    assert ryanair_status["calls"] == 8
    assert ryanair_status["last_error"]
    # The 7 successful destinations' deals are still returned.
    assert len({d.destination for d in results}) == 7
    assert _FAILING_IATA not in {d.destination for d in results}


def test_run_repeatedly_never_loses_the_failure(monkeypatch):
    # The race was intermittent; run the real orchestrator path many times to
    # be confident it never regresses.
    for _ in range(50):
        orch = DealOrchestrator()
        monkeypatch.setattr(orch.registry, "get_reachable", lambda *a, **k: list(_FAKE_DESTS))
        monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", _flaky_ryanair)
        monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **k: [])

        results = orch.search_by_category(
            "european-islands", "BUD", "2026-08-01", "2026-08-03", fresh=True
        )

        ryanair_status = orch.provider_status["ryanair"]
        assert ryanair_status["ok"] is False
        assert ryanair_status["errors"] == 1
        assert len({d.destination for d in results}) == 7
