from flight_deals.orchestrator import DealOrchestrator


def test_orchestrator_initialization():
    orch = DealOrchestrator()
    assert orch is not None


def test_search_by_category_smoke(monkeypatch):
    orch = DealOrchestrator()
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", lambda *a, **k: [])
    monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **k: [])
    results = orch.search_by_category("european-islands", "BUD", "2026-08-01", "2026-08-10")
    assert isinstance(results, list)


def test_provider_exception_surfaces_in_sources(monkeypatch):
    """
    A provider that raises must show up as a failure in `provider_status`
    (what the CLI prints as its `sources:` line) instead of being silently
    swallowed into an empty result indistinguishable from "no deals found".
    """
    orch = DealOrchestrator()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", boom)
    monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **kw: [])

    results = orch.search_by_category(
        category="european-islands",
        origin="BUD",
        date_from="2026-08-01",
        date_to="2026-08-03",
    )

    # A blown-up provider must never crash the whole search...
    assert isinstance(results, list)
    # ...but it must be visible, not indistinguishable from "no deals".
    assert "ryanair" in orch.provider_status
    assert orch.provider_status["ryanair"]["ok"] is False
    assert orch.provider_status["ryanair"]["last_error"]
    assert "wizz" in orch.provider_status
    assert orch.provider_status["wizz"]["ok"] is True


def test_typed_provider_exception_maps_to_status(monkeypatch):
    """A typed SchemaError surfaces as `parse_error` in the aggregated status."""
    from flight_deals.http import SchemaError

    orch = DealOrchestrator()
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights",
                        lambda *a, **k: (_ for _ in ()).throw(SchemaError("drift")))
    monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **k: [])

    orch.search_by_category("seaside", "BUD", "2026-08-22", "2026-08-24")
    assert orch.provider_status["ryanair"]["status"] == "parse_error"
