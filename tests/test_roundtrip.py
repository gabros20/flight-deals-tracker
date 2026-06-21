import pytest
from flight_deals.orchestrator import DealOrchestrator

def test_find_roundtrip_deals_returns_list():
    orch = DealOrchestrator()
    pairs = orch.find_roundtrip_deals(
        origin="BUD",
        destination="ALC",
        outbound_from="2026-08-01",
        outbound_to="2026-08-10",
        return_from="2026-08-15",
        return_to="2026-08-25",
    )
    assert isinstance(pairs, list)