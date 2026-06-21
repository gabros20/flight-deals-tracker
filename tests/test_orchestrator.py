import pytest
from flight_deals.orchestrator import DealOrchestrator

def test_orchestrator_initialization():
    orch = DealOrchestrator()
    assert orch is not None

def test_search_by_category_smoke():
    orch = DealOrchestrator()
    # This will return empty or real results depending on dates
    results = orch.search_by_category(
        "european-islands", "BUD", "2026-08-01", "2026-08-10"
    )
    assert isinstance(results, list)