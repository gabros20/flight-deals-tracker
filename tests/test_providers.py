"""RyanairProvider basic construction + fixture-backed method smoke.

Live network is never touched (Global Constraint 10) — see
test_ryanair_provider.py for the full fixture-driven coverage.
"""

import responses

from flight_deals.providers import ryanair as ry
from flight_deals.providers.ryanair import RyanairProvider

from conftest import load_body


def test_ryanair_provider_initialization():
    provider = RyanairProvider(use_cache=False)
    assert provider is not None
    assert provider.name == "ryanair"


@responses.activate
def test_ryanair_cheapest_per_day_smoke():
    url = ry.FARFND_ONEWAY_CPD.format(origin="BUD", dest="CFU")
    responses.add(responses.GET, url,
                  json=load_body("farfnd_cheapest_per_day_bud_cfu.json"), status=200)
    results = RyanairProvider(use_cache=False).cheapest_per_day("BUD", "CFU", "2026-08")
    assert isinstance(results, list) and results
