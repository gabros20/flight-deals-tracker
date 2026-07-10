"""Wizz provider tests — fixtures only, via the `responses` lib (Task 4).

Covers: happy path both directions incl. HUF->EUR conversion; a version-drift
404 -> HTML re-scrape -> retry-once success reporting version_refreshed;
discovery failure -> ProviderDown; unknown currency -> typed error; schema drift.
"""

import json
import threading
from pathlib import Path

import pytest
import responses

from flight_deals import fx, http
from flight_deals.fx import UnknownCurrency
from flight_deals.http import ProviderDown, SchemaError
from flight_deals.providers import wizz as wizz_mod
from flight_deals.providers.wizz import (
    TIMETABLE_URL,
    VERSION_PAGE_URL,
    WizzProvider,
)

from conftest import load_body

FIXTURES = Path(__file__).parent / "fixtures"


def _p():
    return WizzProvider(use_cache=False)


def _timetable_url():
    # Version resolves offline (seed file / fallback) — the URL both directions
    # of the happy path use.
    return TIMETABLE_URL.format(version=WizzProvider.FALLBACK_VERSION)


# --------------------------------------------------------------------------- #
# Happy path — both directions + HUF->EUR                                      #
# --------------------------------------------------------------------------- #
@responses.activate
def test_timetable_happy_both_directions_and_fx(tmp_path, monkeypatch):
    # Pin HUF=395 via fx's own test-reset mechanism (swapped-in rate table +
    # reload_rates()) rather than relying on the committed data/fx_rates.json
    # seed's numeric value — that seed is refreshed independently from live
    # ECB rates (scripts/refresh_fx.py), so this test must not break when it is.
    f = tmp_path / "fx_rates.json"
    f.write_text(json.dumps({"schema_version": 1, "base": "EUR", "as_of": "2026-07-01",
                             "rates": {"HUF": 395.0}}))
    monkeypatch.setattr(fx, "resolve_path", lambda _p: f)
    fx.reload_rates()

    responses.add(responses.POST, _timetable_url(),
                  json=load_body("wizz_timetable_bud_cta.json"), status=200)
    result = _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")

    assert result.version_refreshed is False
    assert result.outbound and result.inbound  # both directions parsed
    assert all(f.origin == "BUD" and f.destination == "CTA" for f in result.outbound)
    assert all(f.origin == "CTA" and f.destination == "BUD" for f in result.inbound)

    f0 = result.outbound[0]
    assert f0.carrier == "wizzair"
    assert f0.price_confidence == "approximate"
    assert f0.currency_original == "HUF"
    assert f0.source_endpoint == "wizz/timetable"
    # 26090 HUF at the pinned rate 395 -> EUR
    assert f0.price_eur == round(26090.0 / 395.0, 2) == 66.05
    # Everything normalized to EUR (nothing non-EUR leaks past the boundary).
    assert all(fare.price_eur > 0 for fare in result.outbound + result.inbound)


@responses.activate
def test_timetable_tuple_contract():
    responses.add(responses.POST, _timetable_url(),
                  json=load_body("wizz_timetable_bud_cta.json"), status=200)
    out, ret = _p().timetable("BUD", "CTA", "2026-08-22", "2026-09-05")
    assert len(out) == 10 and len(ret) == 10


# --------------------------------------------------------------------------- #
# Version drift 404 -> re-scrape -> retry once -> success                      #
# --------------------------------------------------------------------------- #
@responses.activate
def test_version_drift_404_rescrape_retry_once():
    url = _timetable_url()
    html = (FIXTURES / "wizz_version_discovery_snippet.html").read_text()
    # First POST: the pinned version 404s (drift). Then discovery GET returns the
    # page HTML containing be.wizzair.com/29.6.0. Retry POST: 200.
    responses.add(responses.POST, url, body="<html>404</html>", status=404)
    responses.add(responses.GET, VERSION_PAGE_URL, body=html, status=200)
    responses.add(responses.POST, url,
                  json=load_body("wizz_timetable_bud_cta.json"), status=200)

    result = _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")
    assert result.version_refreshed is True
    assert result.outbound and result.inbound
    # Exactly one re-scrape + one retry (3 calls total: POST, GET, POST).
    assert len(responses.calls) == 3


@responses.activate
def test_oneway_deals_reports_refresh_flag():
    url = _timetable_url()
    html = (FIXTURES / "wizz_version_discovery_snippet.html").read_text()
    responses.add(responses.POST, url, body="x", status=404)
    responses.add(responses.GET, VERSION_PAGE_URL, body=html, status=200)
    responses.add(responses.POST, url,
                  json=load_body("wizz_timetable_bud_cta.json"), status=200)

    deals, refreshed = _p().oneway_deals("BUD", "CTA", "2026-08-22", "2026-09-05")
    assert refreshed is True
    assert deals and all(d.source == "wizz" and d.currency == "EUR" for d in deals)
    assert all(d.source_details.get("price_confidence") == "approximate" for d in deals)


# --------------------------------------------------------------------------- #
# Discovery failure -> ProviderDown                                           #
# --------------------------------------------------------------------------- #
@responses.activate
def test_discovery_failure_raises_provider_down():
    url = _timetable_url()
    responses.add(responses.POST, url, body="x", status=404)
    # Page returns no be.wizzair.com/X.Y.Z anywhere -> discovery fails.
    responses.add(responses.GET, VERSION_PAGE_URL, body="<html>nothing here</html>", status=200)
    with pytest.raises(ProviderDown):
        _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")


@responses.activate
def test_retry_still_404_propagates():
    """If the freshly discovered version ALSO 404s, it propagates (retry once)."""
    url = _timetable_url()
    html = (FIXTURES / "wizz_version_discovery_snippet.html").read_text()
    responses.add(responses.POST, url, body="x", status=404)
    responses.add(responses.GET, VERSION_PAGE_URL, body=html, status=200)
    responses.add(responses.POST, url, body="x", status=404)  # retry also fails
    with pytest.raises(ProviderDown):
        _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")


# --------------------------------------------------------------------------- #
# Unknown currency -> typed error                                             #
# --------------------------------------------------------------------------- #
@responses.activate
def test_unknown_currency_errors_not_silent():
    body = {
        "outboundFlights": [{
            "departureStation": "BUD", "arrivalStation": "CTA",
            "departureDate": "2026-08-23T00:00:00",
            "price": {"amount": 100.0, "currencyCode": "XYZ"},
        }],
        "returnFlights": [],
    }
    responses.add(responses.POST, _timetable_url(), json=body, status=200)
    with pytest.raises(UnknownCurrency):
        _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")


# --------------------------------------------------------------------------- #
# Schema drift / transport                                                    #
# --------------------------------------------------------------------------- #
@responses.activate
def test_schema_drift_raises_schema_error():
    responses.add(responses.POST, _timetable_url(), json={"totally": "different"}, status=200)
    with pytest.raises(SchemaError):
        _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")


@responses.activate
def test_empty_directions_is_not_an_error():
    responses.add(responses.POST, _timetable_url(),
                  json={"outboundFlights": [], "returnFlights": []}, status=200)
    result = _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")
    assert result.outbound == [] and result.inbound == []


@responses.activate
def test_5xx_raises_provider_down():
    url = _timetable_url()
    for _ in range(4):
        responses.add(responses.POST, url, json={}, status=503)
    with pytest.raises(ProviderDown):
        _p().search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05")


@responses.activate
def test_oneway_deals_filters_window_and_converts():
    responses.add(responses.POST, _timetable_url(),
                  json=load_body("wizz_timetable_bud_cta.json"), status=200)
    deals, _ = _p().oneway_deals("BUD", "CTA", "2026-08-23", "2026-08-27")
    assert deals
    assert all(d.currency == "EUR" for d in deals)
    assert all("2026-08-23" <= d.departure_date <= "2026-08-27" for d in deals)


# --------------------------------------------------------------------------- #
# Concurrent version discovery is deduplicated (fix-wave regression)          #
# --------------------------------------------------------------------------- #
@responses.activate
def test_concurrent_drift_discovers_once(tmp_path, monkeypatch):
    """
    Two workers both POST with the same stale (pinned) version and both get a
    version-drift 404. Before the fix, `_discover_version` re-scraped the
    timetable page unlocked with no re-check, so BOTH workers would hit the
    HTML page. The fix holds `_version_lock` across the whole
    check-then-scrape-then-persist section: whoever gets the lock first
    re-scrapes and persists; the other wakes up, sees `_version_cache` already
    advanced past the stale version *it* POSTed with, and reuses it directly.

    Isolates `data/wizz_version.txt` to a tmp file (via `resolve_path`) so a
    genuine version change (29.6.0 -> 29.9.9, unlike the other drift tests
    which coincidentally rediscover the same fallback) never touches the
    committed file.
    """
    stale_version = WizzProvider.FALLBACK_VERSION  # "29.6.0"
    new_version = "29.9.9"
    stale_url = TIMETABLE_URL.format(version=stale_version)
    new_url = TIMETABLE_URL.format(version=new_version)
    html = f"<html>...be.wizzair.com/{new_version}...</html>"
    body = load_body("wizz_timetable_bud_cta.json")

    version_file = tmp_path / "wizz_version.txt"
    version_file.write_text(stale_version + "\n")
    monkeypatch.setattr(wizz_mod, "resolve_path", lambda _p: version_file)

    # Single registered entry per URL is reused for every matching call
    # (responses' FirstMatchRegistry), so this covers both workers' attempts.
    responses.add(responses.POST, stale_url, body="drift", status=404)
    responses.add(responses.GET, VERSION_PAGE_URL, body=html, status=200)
    responses.add(responses.POST, new_url, json=body, status=200)

    # Barrier lines up both workers on the SAME stale cached version (read
    # *before* either can race ahead and refresh it) so they genuinely both
    # drift on it concurrently — not a timing gamble, the barrier forces it.
    barrier = threading.Barrier(2)
    real_current_version = WizzProvider._current_version

    def synced_current_version(self):
        v = real_current_version(self)
        barrier.wait(timeout=5)
        return v

    monkeypatch.setattr(WizzProvider, "_current_version", synced_current_version)

    results, errors = [], []

    def worker():
        try:
            p = WizzProvider(use_cache=False)
            results.append(p.search_timetable("BUD", "CTA", "2026-08-22", "2026-09-05"))
        except Exception as e:  # pragma: no cover - surfaced via `errors`
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(results) == 2
    assert all(r.version_refreshed for r in results)
    assert all(r.outbound and r.inbound for r in results)

    get_calls = [c for c in responses.calls if c.request.url == VERSION_PAGE_URL]
    assert len(get_calls) == 1, "discovery GET must happen exactly once, not once per worker"
    assert version_file.read_text().strip() == new_version
