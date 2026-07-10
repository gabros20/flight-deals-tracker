"""fx.to_eur tests — currency normalization at the provider boundary (Task 4)."""

import json

import pytest

from flight_deals import fx
from flight_deals.fx import UnknownCurrency
from flight_deals.http import ProviderError


def test_eur_passthrough():
    fx.reload_rates()
    assert fx.to_eur(123.45, "EUR") == 123.45
    assert fx.to_eur(100, "eur") == 100.0  # case-insensitive


def test_huf_converts_to_eur():
    fx.reload_rates()
    # seed rate is EUR/HUF = 395 -> 1 EUR = 395 HUF
    assert fx.to_eur(26090.0, "HUF") == round(26090.0 / 395.0, 2)
    assert fx.to_eur(26090.0, "HUF") == 66.05


def test_known_currencies_present():
    for code in ("HUF", "PLN", "CZK", "RON", "GBP", "CHF"):
        assert code in fx.known_currencies()


def test_unknown_currency_raises_typed_error():
    fx.reload_rates()
    with pytest.raises(UnknownCurrency):
        fx.to_eur(100.0, "XYZ")
    # UnknownCurrency is a ProviderError so the orchestrator surfaces it as a
    # provider failure (never a silent, unconverted pass-through).
    assert issubclass(UnknownCurrency, ProviderError)


def test_empty_currency_raises():
    with pytest.raises(UnknownCurrency):
        fx.to_eur(100.0, "")


def test_staleness_warning(tmp_path, monkeypatch, caplog):
    """A >30-day-old table logs a warning but still converts."""
    stale = {
        "schema_version": 1, "base": "EUR", "as_of": "2000-01-01",
        "rates": {"HUF": 400.0},
    }
    f = tmp_path / "fx_rates.json"
    f.write_text(json.dumps(stale))
    monkeypatch.setattr(fx, "resolve_path", lambda _p: f)
    fx.reload_rates()
    import logging
    with caplog.at_level(logging.WARNING):
        val = fx.to_eur(400.0, "HUF")
    assert val == 1.0
    assert any("days old" in r.message for r in caplog.records)
    # conftest's autouse fixture resets fx._TABLE so the next test reloads the
    # real committed table (resolve_path is un-patched by then).
