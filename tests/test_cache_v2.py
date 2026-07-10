"""Cache v2: param-hash keys, per-endpoint TTLs, atomic writes, use_cache=False."""

from pathlib import Path

import responses
from freezegun import freeze_time

from flight_deals.cache import ResponseCache
from flight_deals.providers import ryanair as ry
from flight_deals.providers.ryanair import RyanairProvider

from conftest import load_body


def _cache(tmp_path) -> ResponseCache:
    # explicit TTLs so tests don't depend on config: search 30m, calendar 6h, routes 7d
    return ResponseCache(cache_dir=tmp_path, ttls={"search": 1800, "calendar": 21600, "routes": 604800})


def test_key_includes_all_params(tmp_path):
    c = _cache(tmp_path)
    k1 = c._key("ryanair", "roundTripFares", {"o": "BUD", "durationFrom": 5, "ret": "2026-08-27"})
    k2 = c._key("ryanair", "roundTripFares", {"o": "BUD", "durationFrom": 8, "ret": "2026-08-27"})
    k3 = c._key("ryanair", "roundTripFares", {"ret": "2026-08-27", "o": "BUD", "durationFrom": 5})
    assert k1 != k2           # duration is part of the key
    assert k1 == k3           # order-independent


def test_set_get_roundtrip(tmp_path):
    c = _cache(tmp_path)
    params = {"o": "BUD", "d": "CFU"}
    assert c.get("ryanair", "roundTripFares", params) is None
    c.set("ryanair", "roundTripFares", params, {"fares": []})
    assert c.get("ryanair", "roundTripFares", params) == {"fares": []}


def test_atomic_write_leaves_no_tmp(tmp_path):
    c = _cache(tmp_path)
    c.set("ryanair", "routes", {"origin": "BUD"}, ["CFU", "CTA"])
    files = list(Path(tmp_path).iterdir())
    assert len(files) == 1
    assert not any(".tmp" in f.name for f in files)


def test_per_endpoint_ttl_expiry(tmp_path):
    c = _cache(tmp_path)
    with freeze_time("2026-07-10 12:00:00"):
        c.set("ryanair", "roundTripFares", {"x": 1}, {"fares": [1]})   # search: 30m TTL
        c.set("ryanair", "routes", {"origin": "BUD"}, ["CFU"])          # routes: 7d TTL
    # 45 minutes later: search entry expired, routes entry still fresh
    with freeze_time("2026-07-10 12:45:00"):
        assert c.get("ryanair", "roundTripFares", {"x": 1}) is None
        assert c.get("ryanair", "routes", {"origin": "BUD"}) == ["CFU"]
    # 8 days later: routes entry expired too
    with freeze_time("2026-07-18 12:00:00"):
        assert c.get("ryanair", "routes", {"origin": "BUD"}) is None


@responses.activate
def test_provider_uses_cache_then_serves_from_it(tmp_path):
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_exact_bud_cfu.json"), status=200)
    p = RyanairProvider(use_cache=True)
    p._cache = _cache(tmp_path)  # isolate to temp dir

    kwargs = dict(out_from="2026-08-22", out_to="2026-08-22", ret_from="2026-08-27", ret_to="2026-08-27")
    a = p.roundtrip_fares("BUD", "CFU", **kwargs)
    b = p.roundtrip_fares("BUD", "CFU", **kwargs)  # served from cache
    assert len(a) == len(b) == 1
    assert len(responses.calls) == 1  # second call hit cache, not network


@responses.activate
def test_use_cache_false_always_refetches(tmp_path):
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_exact_bud_cfu.json"), status=200)
    responses.add(responses.GET, ry.FARFND_ROUNDTRIP,
                  json=load_body("farfnd_roundtrip_exact_bud_cfu.json"), status=200)
    p = RyanairProvider(use_cache=True)
    p._cache = _cache(tmp_path)

    kwargs = dict(out_from="2026-08-22", out_to="2026-08-22", ret_from="2026-08-27",
                  ret_to="2026-08-27", use_cache=False)
    p.roundtrip_fares("BUD", "CFU", **kwargs)
    p.roundtrip_fares("BUD", "CFU", **kwargs)
    assert len(responses.calls) == 2  # use_cache=False honoured both times
