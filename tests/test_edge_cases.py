"""Edge case and robustness tests — offline only (Global Constraint 10)."""

import responses

from flight_deals.history import PriceHistoryStore
from flight_deals.providers import ryanair as ry
from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider


@responses.activate
def test_nonexistent_route_returns_empty_not_error():
    """A 200-but-empty response is a valid 'no service' answer, not a failure."""
    url = ry.FARFND_ONEWAY_CPD.format(origin="BUD", dest="ZZZ")
    responses.add(responses.GET, url,
                  json={"outbound": {"fares": []}}, status=200)
    deals = RyanairProvider(use_cache=False).get_cheapest_flights("BUD", "2026-08-01", "2026-08-05", "ZZZ")
    assert deals == []


def test_history_no_previous_price():
    h = PriceHistoryStore()
    assert h.get_previous_price("XXX", "YYY", "2099-01-01") is None


def test_wizz_provider_initialization():
    """Wizz constructs with NO network I/O; the version resolves offline from
    the persisted file / fallback constant (only re-discovered on a drift 404)."""
    import re
    w = WizzProvider(use_cache=False)
    v = w._current_version()
    assert isinstance(v, str) and re.match(r"^\d+\.\d+\.\d+$", v)
