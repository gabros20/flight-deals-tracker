"""
Currency normalization to EUR at the provider boundary (Task 4 / Global
Constraint 4).

EUR is the canonical money unit across the whole system: stats, thresholds,
history and ``--budget``/``--max-price`` are all EUR. Some providers return a
market currency — the Wizz timetable returns **HUF for BUD** — so every
non-EUR fare is converted here, once, before it can enter results or averages.
One HUF row must never poison an average.

Rates live in a committed seed ``data/fx_rates.json`` (EUR-base: ``1 EUR = rate
units of the foreign currency``), refreshed out-of-band by
``scripts/refresh_fx.py`` (frankfurter.app / ECB) — never in the request path.
A stale table (>30d) logs a warning but still converts; an **unknown currency
raises** ``UnknownCurrency`` rather than silently passing a wrong number
through (Global Constraint 3: no silent failures).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Dict, Optional

from flight_deals.http import ProviderError
from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)

FX_RATES_FILE = "data/fx_rates.json"
STALE_AFTER_DAYS = 30


class UnknownCurrency(ProviderError):
    """
    A currency with no rate in ``fx_rates.json``. A typed error (not a silent
    pass-through) so the orchestrator surfaces it as a provider failure instead
    of letting an unconverted, wrong-magnitude number into stats/thresholds.
    """


class _RateTable:
    """Loaded-once, mutable-for-tests view of the fx seed file."""

    def __init__(self) -> None:
        self.base: str = "EUR"
        self.as_of: Optional[str] = None
        self.rates: Dict[str, float] = {}
        self._loaded = False
        self._warned_stale = False

    def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        path = resolve_path(FX_RATES_FILE)
        data = json.loads(path.read_text())
        self.base = str(data.get("base", "EUR")).upper()
        self.as_of = data.get("as_of")
        self.rates = {str(k).upper(): float(v) for k, v in (data.get("rates") or {}).items()}
        self._loaded = True
        self._warned_stale = False

    def _check_staleness(self) -> None:
        if self._warned_stale or not self.as_of:
            return
        try:
            as_of = date.fromisoformat(str(self.as_of)[:10])
        except ValueError:
            return
        age = (datetime.now(timezone.utc).date() - as_of).days
        if age > STALE_AFTER_DAYS:
            logger.warning(
                "fx: rate table is %d days old (as_of %s > %dd) — run scripts/refresh_fx.py",
                age, self.as_of, STALE_AFTER_DAYS,
            )
            self._warned_stale = True


_TABLE = _RateTable()


def reload_rates() -> None:
    """Force a re-read of the seed file (used by refresh tooling and tests)."""
    _TABLE.load(force=True)


def to_eur(amount: float, currency: str) -> float:
    """
    Convert ``amount`` in ``currency`` to EUR.

    * EUR (or the table base) passes through unchanged.
    * Known currency -> ``amount / rate`` (the table is EUR-base:
      ``1 EUR = rate * currency``).
    * Unknown currency -> ``UnknownCurrency`` (never a silent pass-through).
    """
    if amount is None:
        raise ValueError("fx.to_eur: amount is None")
    code = (currency or "").strip().upper()
    if not code:
        raise UnknownCurrency("fx: empty/None currency code")

    _TABLE.load()
    if code == _TABLE.base:
        return round(float(amount), 2)

    _TABLE._check_staleness()

    rate = _TABLE.rates.get(code)
    if rate is None:
        raise UnknownCurrency(
            f"fx: no EUR rate for {code!r} in {FX_RATES_FILE} "
            f"(known: {', '.join(sorted(_TABLE.rates)) or 'none'})"
        )
    if rate <= 0:
        raise UnknownCurrency(f"fx: non-positive rate for {code!r}: {rate}")
    return round(float(amount) / rate, 2)


def known_currencies() -> list[str]:
    _TABLE.load()
    return sorted([_TABLE.base, *_TABLE.rates.keys()])
