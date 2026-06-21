import pytest
from flight_deals.orchestrator import DealOrchestrator

def test_search_by_category_returns_list():
    orch = DealOrchestrator()
    results = orch.search_by_category(
        category="european-islands",
        origin="BUD",
        date_from="2026-08-01",
        date_to="2026-08-10"
    )
    assert isinstance(results, list)