"""Ryanair provider tests — fixtures only, via the `responses` lib."""

import pytest
import responses

from flight_deals.http import ProviderDown, RateLimited, SchemaError
from flight_deals.providers import ryanair as ry
from flight_deals.providers.ryanair import RyanairProvider

from conftest import load_body


def _p():
    return RyanairProvider(use_cache=False)


# --------------------------------------------------------------------------- #
# RT-EXACT                                                                     #
# --------------------------------------------------------------------------- #
@responses.activate
def test_roundtrip_exact_happy():
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_exact_bud_cfu.json"), status=200)
    pairs = _p().roundtrip_fares("BUD", "CFU", out_from="2026-08-22", out_to="2026-08-22",
                                 ret_from="2026-08-27", ret_to="2026-08-27")
    assert len(pairs) == 1
    fp = pairs[0]
    assert (fp.origin, fp.destination) == ("BUD", "CFU")
    assert fp.total_price_eur == 173.98
    assert fp.nights == 5
    assert fp.price_confidence == "exact"
    assert fp.outbound.flight_number == "FR8054"
    assert fp.outbound.departure_time == "16:10"
    assert fp.inbound.flight_number == "FR8053"


# --------------------------------------------------------------------------- #
# RT-ANYWHERE + duration filtering + truncation tolerance                     #
# --------------------------------------------------------------------------- #
@responses.activate
def test_roundtrip_anywhere_mode():
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_anywhere_bud.json"), status=200)
    pairs = _p().roundtrip_fares("BUD", None, out_from="2026-08-22", out_to="2026-08-24",
                                 duration_from=5, duration_to=8)
    # anywhere -> many distinct destinations; fixture kept 20 (size:65 truncated)
    assert len(pairs) == 20
    assert len({p.destination for p in pairs}) > 1
    # duration filter honoured
    assert all(5 <= p.nights <= 8 for p in pairs)
    # request omitted arrivalAirportIataCode in anywhere mode
    sent = responses.calls[0].request.url
    assert "arrivalAirportIataCode" not in sent


@responses.activate
def test_duration_filter_excludes_out_of_range():
    body = load_body("farfnd_roundtrip_anywhere_bud.json")
    pairs = _p()._parse_roundtrip(body, 7, 7)
    assert pairs, "expected at least one 7-night pair"
    assert all(p.nights == 7 for p in pairs)


# --------------------------------------------------------------------------- #
# OW-ANYWHERE — one-way fares, real captured shape (Task 7 fix wave)          #
# --------------------------------------------------------------------------- #
@responses.activate
def test_oneway_anywhere_mode_real_fixture_shape():
    """Parses the live-captured farfnd oneWayFares anywhere-mode body
    (tests/fixtures/farfnd_oneway_anywhere_bud.json) — a real recorded shape,
    complementing the hand-built synthetic body already exercised by
    test_intents.test_oneway_produces_s1_deals."""
    responses.add(responses.GET, ry.FARFND_ONEWAY,
                  json=load_body("farfnd_oneway_anywhere_bud.json"), status=200)
    fares = _p().oneway_fares("BUD", None, out_from="2026-08-22", out_to="2026-08-24")
    # anywhere -> many distinct destinations; fixture kept 20 (size:65 truncated)
    assert len(fares) == 20
    assert len({f.destination for f in fares}) > 1
    assert all(f.origin == "BUD" for f in fares)
    assert all(f.price_confidence == "exact" for f in fares)
    assert all(f.source_endpoint == "farfnd/oneWayFares" for f in fares)
    # request omitted arrivalAirportIataCode in anywhere mode
    sent = responses.calls[0].request.url
    assert "arrivalAirportIataCode" not in sent


# --------------------------------------------------------------------------- #
# CAL — cheapest per day                                                       #
# --------------------------------------------------------------------------- #
@responses.activate
def test_cheapest_per_day_happy():
    url = ry.FARFND_ONEWAY_CPD.format(origin="BUD", dest="CFU")
    responses.add(responses.GET, url,
                  json=load_body("farfnd_cheapest_per_day_bud_cfu.json"), status=200)
    days = _p().cheapest_per_day("BUD", "CFU", "2026-08")
    # unavailable days are dropped, available ones kept
    assert len(days) == 12
    assert all(d.price_confidence == "exact" for d in days)
    assert all(d.carrier == "ryanair" for d in days)
    assert days[0].date == "2026-08-01"
    assert days[0].currency_original == "EUR"


@responses.activate
def test_cheapest_per_day_other_direction():
    url = ry.FARFND_ONEWAY_CPD.format(origin="CFU", dest="BUD")
    responses.add(responses.GET, url,
                  json=load_body("farfnd_cheapest_per_day_cfu_bud.json"), status=200)
    days = _p().cheapest_per_day("CFU", "BUD", "2026-08")
    assert days and all(d.origin == "CFU" and d.destination == "BUD" for d in days)


# --------------------------------------------------------------------------- #
# routes                                                                       #
# --------------------------------------------------------------------------- #
@responses.activate
def test_routes_happy():
    url = ry.ROUTES_URL.format(origin="BUD")
    responses.add(responses.GET, url, json=load_body("ryanair_routes_bud.json"), status=200)
    codes = _p().routes("BUD")
    assert "AGP" in codes and "BCN" in codes  # present in the captured BUD network
    assert codes == sorted(codes)  # deterministic


@responses.activate
def test_lis_routes_fixture_has_no_azores():
    """Task 18 live finding (captured 2026-07-13): Ryanair serves FNC (Madeira)
    from LIS but does NOT fly to the Azores (PDL/TER) — the Azores are TAP/SATA
    territory. This pins the honest recorded fixture so a future "Azores via LIS"
    S5 assumption can't silently creep in against a real Ryanair route list."""
    url = ry.ROUTES_URL.format(origin="LIS")
    responses.add(responses.GET, url, json=load_body("ryanair_routes_lis.json"), status=200)
    codes = _p().routes("LIS")
    assert "BUD" in codes  # BUD<->LIS is Ryanair-served (LIS is a valid S5 hub)
    assert "FNC" in codes  # Madeira, yes
    assert "PDL" not in codes and "TER" not in codes  # Azores, no


@responses.activate
def test_routes_schema_error_on_non_list():
    url = ry.ROUTES_URL.format(origin="BUD")
    responses.add(responses.GET, url, json={"unexpected": True}, status=200)
    with pytest.raises(SchemaError):
        _p().routes("BUD")


# --------------------------------------------------------------------------- #
# Empty vs. schema drift vs. transport failures                               #
# --------------------------------------------------------------------------- #
@responses.activate
def test_empty_is_not_an_error():
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_empty_nonexistent.json"), status=200)
    pairs = _p().roundtrip_fares("BUD", "JFK", out_from="2026-08-22", out_to="2026-08-22",
                                 ret_from="2026-08-27", ret_to="2026-08-27")
    assert pairs == []  # empty, but no exception — a valid "no service" answer


@responses.activate
def test_schema_drift_raises_schema_error():
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP, json={"totally": "different"}, status=200)
    with pytest.raises(SchemaError):
        _p().roundtrip_fares("BUD", "CFU", out_from="2026-08-22", out_to="2026-08-22",
                             ret_from="2026-08-27", ret_to="2026-08-27")


@responses.activate
def test_5xx_raises_provider_down():
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP, json={}, status=503)
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP, json={}, status=503)
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP, json={}, status=503)
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP, json={}, status=503)
    with pytest.raises(ProviderDown):
        _p().cheapest_per_day("BUD", "CFU", "2026-08")


@responses.activate
def test_get_cheapest_flights_compat_filters_window():
    url = ry.FARFND_ONEWAY_CPD.format(origin="BUD", dest="CFU")
    responses.add(responses.GET, url,
                  json=load_body("farfnd_cheapest_per_day_bud_cfu.json"), status=200)
    deals = _p().get_cheapest_flights("BUD", "2026-08-01", "2026-08-05", "CFU")
    assert deals and all(d.source == "ryanair" for d in deals)
    assert all("2026-08-01" <= d.departure_date <= "2026-08-05" for d in deals)
