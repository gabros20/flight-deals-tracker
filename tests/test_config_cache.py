"""Tests for config and caching features"""

from flight_deals.config import FlightDealsConfig, load_config
from flight_deals.cache import FlightCache
from flight_deals.models import FlightDeal
from datetime import datetime, timedelta
import tempfile
from pathlib import Path


def test_config_defaults():
    cfg = FlightDealsConfig()
    assert cfg.default_origin == "BUD"
    assert cfg.currency == "EUR"
    assert cfg.cache_ttl_hours == 6


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


def test_registry_reachability():
    from flight_deals.registry.destinations import DestinationRegistry
    reg = DestinationRegistry()
    
    reachable = reg.get_reachable("BUD", "european-islands")
    assert len(reachable) > 0
    # Should include known ones
    iatas = {a.iata for a in reachable}
    assert "PMI" in iatas or "CFU" in iatas