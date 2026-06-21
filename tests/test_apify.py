import pytest
from unittest.mock import patch, MagicMock
from flight_deals.providers.apify import ApifyProvider
from flight_deals.models import FlightDeal
from flight_deals.config import FlightDealsConfig


def test_apify_provider_no_token_returns_empty():
    """Without token, provider should not call API and return empty list."""
    config = FlightDealsConfig(apify_token=None, apify_enabled=True)
    provider = ApifyProvider(config=config)
    results = provider.get_cheapest_flights("BUD", "2026-08-01", "2026-08-10", "PMI")
    assert results == []


def test_apify_provider_graceful_skip_when_disabled():
    config = FlightDealsConfig(apify_token="fake", apify_enabled=False)
    provider = ApifyProvider(config=config)
    results = provider.get_cheapest_flights("BUD", "2026-08-01", "2026-08-10")
    assert results == []


@patch("flight_deals.providers.apify.requests.post")
def test_apify_provider_parses_results(mock_post):
    """Mock Apify response and verify normalization to FlightDeal with stops."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "bestPrice": 89.5,
            "currency": "EUR",
            "cheapestSource": "google_flights",
            "segments": [
                {"from": "BUD", "to": "VIE"},
                {"from": "VIE", "to": "PMI"}
            ],
            "isSelfTransfer": True,
            "bookingLinks": {"google_flights": "https://example.com"}
        }
    ]
    mock_post.return_value = mock_response
    mock_post.return_value.status_code = 200

    config = FlightDealsConfig(apify_token="test_token", apify_enabled=True)
    provider = ApifyProvider(config=config, use_cache=False)

    results = provider.get_cheapest_flights("BUD", "2026-08-01", "2026-08-10", "PMI")

    assert len(results) == 1
    deal = results[0]
    assert isinstance(deal, FlightDeal)
    assert deal.source == "apify:google_flights"
    assert deal.price == 89.5
    assert deal.stops == 1
    assert deal.source_details.get("isSelfTransfer") is True
    assert "booking_url" in deal.model_fields_set or deal.booking_url is not None


def test_apify_provider_handles_empty_api_response():
    with patch("flight_deals.providers.apify.requests.post") as mock_post:
        mock_post.return_value.json.return_value = []
        config = FlightDealsConfig(apify_token="test", apify_enabled=True)
        provider = ApifyProvider(config=config, use_cache=False)
        results = provider.get_cheapest_flights("BUD", "2026-08-01", "2026-08-10")
        assert results == []
