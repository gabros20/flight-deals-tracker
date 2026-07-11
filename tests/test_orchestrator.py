"""DealOrchestrator is now a thin provider shell (the legacy
``search_by_category`` + cross-carrier merge path was removed; the planner is
the single search path). What remains worth testing here is the shared status
merge primitives (``status_for_exception`` / ``aggregate_status``) that the
planner's execute loop drives — the failure-visibility semantics that used to be
asserted through ``search_by_category`` are ported to direct event-merge tests
(and to the real planner path in tests/test_concurrency.py)."""

from flight_deals.orchestrator import DealOrchestrator, aggregate_status, status_for_exception


def test_orchestrator_initialization():
    orch = DealOrchestrator()
    assert orch is not None
    # The shell keeps exactly the shared provider instances `track` drives.
    assert orch.ryanair is not None and orch.wizz is not None


def test_typed_provider_exception_maps_to_status():
    """A typed SchemaError surfaces as `parse_error`; a ProviderDown as `error`."""
    from flight_deals.http import ProviderDown, SchemaError

    assert status_for_exception(SchemaError("drift")) == "parse_error"
    assert status_for_exception(ProviderDown("down")) == "error"


def test_wizz_version_refreshed_surfaces_and_stays_ok():
    """A `version_refreshed` event keeps the provider ok while noting the refresh."""
    status = aggregate_status([{"provider": "wizzair", "status": "version_refreshed"}])
    assert status["wizzair"]["ok"] is True
    assert status["wizzair"]["status"] == "version_refreshed"


def test_wizz_error_beats_version_refreshed():
    """A real failure on any call wins over a version_refreshed elsewhere,
    regardless of the order the concurrent events are merged in."""
    events = [
        {"provider": "wizzair", "status": "version_refreshed"},
        {"provider": "wizzair", "status": "error", "detail": "down"},
    ]
    for ordering in (events, list(reversed(events))):
        status = aggregate_status(ordering)
        assert status["wizzair"]["ok"] is False
        assert status["wizzair"]["status"] == "error"
        assert status["wizzair"]["last_error"] == "down"
