from flight_deals.orchestrator import DealOrchestrator


def test_orchestrator_initialization():
    orch = DealOrchestrator()
    assert orch is not None


def test_search_by_category_smoke(monkeypatch):
    orch = DealOrchestrator()
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", lambda *a, **k: [])
    monkeypatch.setattr(orch.wizz, "oneway_deals", lambda *a, **k: ([], False))
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
    monkeypatch.setattr(orch.wizz, "oneway_deals", lambda *a, **kw: ([], False))

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
    monkeypatch.setattr(orch.wizz, "oneway_deals", lambda *a, **k: ([], False))

    orch.search_by_category("seaside", "BUD", "2026-08-22", "2026-08-24")
    assert orch.provider_status["ryanair"]["status"] == "parse_error"


def test_wizz_version_refreshed_surfaces_in_sources(monkeypatch):
    """When Wizz auto-refreshes its API version, `sources` says so (and stays ok)."""
    orch = DealOrchestrator()
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", lambda *a, **k: [])
    monkeypatch.setattr(orch.wizz, "oneway_deals", lambda *a, **k: ([], True))

    orch.search_by_category("seaside", "BUD", "2026-08-22", "2026-08-24")
    assert orch.provider_status["wizz"]["ok"] is True
    assert orch.provider_status["wizz"]["status"] == "version_refreshed"


def test_wizz_error_beats_version_refreshed(monkeypatch):
    """A real Wizz failure on any route wins over a version_refreshed elsewhere."""
    from flight_deals.http import ProviderDown

    orch = DealOrchestrator()
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], True          # one route refreshed the version
        raise ProviderDown("down")   # another route is down

    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", lambda *a, **k: [])
    monkeypatch.setattr(orch.wizz, "oneway_deals", flaky)

    orch.search_by_category("seaside", "BUD", "2026-08-22", "2026-08-24")
    assert orch.provider_status["wizz"]["ok"] is False
    assert orch.provider_status["wizz"]["status"] == "error"


def test_cross_carrier_merge_keeps_cheaper(monkeypatch):
    """Same route+date on both carriers -> only the cheaper survives, tagged."""
    from flight_deals.models import FlightDeal

    orch = DealOrchestrator()
    monkeypatch.setattr(orch.registry, "get_reachable",
                        lambda *a, **k: [type("D", (), {"iata": "CTA"})()])

    def ry(*a, **k):
        return [FlightDeal(origin="BUD", destination="CTA", departure_date="2026-08-23",
                           price=80.0, currency="EUR", source="ryanair")]

    def wz(*a, **k):
        return ([FlightDeal(origin="BUD", destination="CTA", departure_date="2026-08-23",
                            price=50.0, currency="EUR", source="wizz")], False)

    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", ry)
    monkeypatch.setattr(orch.wizz, "oneway_deals", wz)

    results = orch.search_by_category("seaside", "BUD", "2026-08-23", "2026-08-23")
    same = [d for d in results if d.destination == "CTA" and d.departure_date == "2026-08-23"]
    assert len(same) == 1
    assert same[0].source == "wizz" and same[0].price == 50.0
