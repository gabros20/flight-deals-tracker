from flight_deals.orchestrator import DealOrchestrator


def test_search_by_category_returns_list(monkeypatch):
    orch = DealOrchestrator()
    monkeypatch.setattr(orch.ryanair, "get_cheapest_flights", lambda *a, **k: [])
    monkeypatch.setattr(orch.wizz, "get_cheapest_flights", lambda *a, **k: [])
    results = orch.search_by_category(
        category="european-islands",
        origin="BUD",
        date_from="2026-08-01",
        date_to="2026-08-10",
    )
    assert isinstance(results, list)
