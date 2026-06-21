import pytest
from flight_deals.providers.ryanair import RyanairProvider

def test_ryanair_provider_initialization():
    provider = RyanairProvider()
    assert provider is not None

def test_ryanair_get_cheapest_flights_smoke():
    provider = RyanairProvider()
    # Smoke test - should not crash on a known route
    results = provider.get_cheapest_flights("BUD", "2026-07-01", "2026-07-10")
    assert isinstance(results, list)