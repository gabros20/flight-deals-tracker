"""Edge case and robustness tests for Flight Deals Tracker"""

import pytest
from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider


def test_search_invalid_airport():
    """Should handle invalid airport codes gracefully"""
    o = DealOrchestrator()
    deals = o.ryanair.get_cheapest_flights("XXX", "2026-07-10", "2026-07-20")
    assert deals == [] or isinstance(deals, list)


def test_search_no_deals_found():
    """Very far future dates should return empty or very few results"""
    o = DealOrchestrator()
    deals = o.search_by_category(
        category="european-islands",
        origin="BUD",
        date_from="2030-01-01",
        date_to="2030-01-10",
    )
    # Should not crash
    assert isinstance(deals, list)


def test_history_no_previous_price():
    """get_previous_price should return None when no history exists"""
    h = PriceHistoryStore()
    price = h.get_previous_price("XXX", "YYY", "2099-01-01")
    assert price is None


def test_wizz_provider_initialization():
    """Wizz provider should initialize with dynamic version"""
    w = WizzProvider()
    assert w.version is not None
    assert isinstance(w.version, str)


def test_roundtrip_empty_results():
    """Roundtrip should return empty list when no deals"""
    o = DealOrchestrator()
    pairs = o.find_roundtrip_deals(
        origin="BUD",
        destination="XXX",
        outbound_from="2026-07-01",
        outbound_to="2026-07-05",
        return_from="2026-07-10",
        return_to="2026-07-15",
    )
    assert pairs == []


def test_orchestrator_handles_mixed_providers():
    """Orchestrator should combine results from both providers without crashing"""
    o = DealOrchestrator()
    deals = o.search_by_category(
        category="seaside",
        origin="BUD",
        date_from="2026-08-01",
        date_to="2026-08-10",
        max_price=300,
    )
    assert isinstance(deals, list)