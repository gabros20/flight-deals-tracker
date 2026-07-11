"""Tests for config and caching features"""

from flight_deals.config import FlightDealsConfig, load_config, save_user_config
import flight_deals.config as config_module
from flight_deals.cache import FlightCache
from flight_deals.models import FlightDeal
from datetime import datetime, timedelta
import tempfile
from pathlib import Path


def test_config_defaults():
    cfg = FlightDealsConfig()
    assert cfg.default_origin == "BUD"
    assert cfg.currency == "EUR"
    # cache_ttl_hours is a float (see docs/UPGRADE-PLAN.md: the field used to
    # be typed `int` with a 0.25 default, which pydantic silently truncated
    # to 0 whenever the value was round-tripped through validation).
    assert cfg.cache_ttl_hours == 0.25
    assert isinstance(cfg.cache_ttl_hours, float)


def test_config_load():
    cfg = load_config()
    assert isinstance(cfg, FlightDealsConfig)
    assert cfg.max_workers > 0


def test_cache_basic():
    with tempfile.TemporaryDirectory() as tmp:
        cache = FlightCache(cache_dir=Path(tmp), ttl_hours=1)
        
        deal = FlightDeal(
            origin="BUD", destination="PMI", departure_date="2026-08-01",
            price=49.0, currency="EUR", source="ryanair"
        )
        
        cache.set("ryanair", "BUD", "2026-07-20", "2026-08-05", [deal], "PMI")
        retrieved = cache.get("ryanair", "BUD", "2026-07-20", "2026-08-05", "PMI")
        
        assert retrieved is not None
        assert len(retrieved) == 1
        assert retrieved[0].price == 49.0


def test_cache_ttl_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        # Use negative TTL to force expiry
        cache = FlightCache(cache_dir=Path(tmp), ttl_hours=-1)
        
        deal = FlightDeal(
            origin="BUD", destination="CFU", departure_date="2026-08-10",
            price=89.0, currency="EUR", source="wizz"
        )
        
        cache.set("wizz", "BUD", "2026-08-01", "2026-08-15", [deal])
        retrieved = cache.get("wizz", "BUD", "2026-08-01", "2026-08-15")
        
        # Should be expired immediately
        assert retrieved is None


def test_cache_ttl_hours_accepts_fractional_values():
    """cache_ttl_hours must be a float field, not int (which silently
    truncated the 0.25/15-minute default to 0)."""
    cfg = FlightDealsConfig(cache_ttl_hours=0.25)
    assert cfg.cache_ttl_hours == 0.25


def test_config_default_round_trips_through_save_and_load(monkeypatch, tmp_path):
    """Saving the default config and reloading it must reproduce the same
    (non-secret) values — proves save/load aren't lossy or lying about what's
    persisted. Uses a temp config path so it never touches the developer's
    real ~/.config/flight-deals/config.json."""
    fake_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_path", lambda: fake_path)

    original = FlightDealsConfig()
    save_user_config(original)

    assert fake_path.exists()

    reloaded_data = load_config()
    assert reloaded_data.default_origin == original.default_origin
    assert reloaded_data.currency == original.currency
    assert reloaded_data.cache_ttl_hours == original.cache_ttl_hours
    assert reloaded_data.max_workers == original.max_workers


def test_save_user_config_never_persists_secrets(monkeypatch, tmp_path):
    """Secrets are env-only (Global Constraint #8) — saving a config that
    happens to carry a token/chat-id in memory must not write it to disk."""
    fake_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_path", lambda: fake_path)

    cfg = FlightDealsConfig(
        telegram_bot_token="secret-token",
        telegram_chat_id="secret-chat-id",
    )
    save_user_config(cfg)

    saved_text = fake_path.read_text()
    assert "secret-token" not in saved_text
    assert "secret-chat-id" not in saved_text


def test_legacy_config_with_removed_keys_still_loads_with_warning(monkeypatch, tmp_path, caplog):
    """A config.json written before enable_cache/apify_*/alerts_log_path were
    removed must still load (unknown keys ignored) with a warning, never brick."""
    import json as _json
    import logging

    fake_path = tmp_path / "config.json"
    fake_path.write_text(_json.dumps({
        "default_origin": "VIE",
        "enable_cache": True,
        "apify_token": "old-secret",
        "apify_enabled": False,
        "alerts_log_path": "data/price_alerts.csv",
    }))
    monkeypatch.setattr(config_module, "get_config_path", lambda: fake_path)
    # Point the project-config lookup somewhere empty so only our file is read.
    monkeypatch.setattr(config_module, "get_project_root", lambda: tmp_path / "noproj")

    with caplog.at_level(logging.WARNING, logger="flight_deals.config"):
        cfg = load_config()

    assert cfg.default_origin == "VIE"           # known key survives
    assert not hasattr(cfg, "enable_cache")      # removed key is gone
    assert "unknown/removed config keys" in caplog.text
    assert "apify_token" in caplog.text


def test_registry_reachability():
    from flight_deals.registry.destinations import DestinationRegistry
    reg = DestinationRegistry()
    
    reachable = reg.get_reachable("BUD", "european-islands")
    assert len(reachable) > 0
    # Should include known ones
    iatas = {a.iata for a in reachable}
    assert "PMI" in iatas or "CFU" in iatas