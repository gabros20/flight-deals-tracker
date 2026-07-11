#!/usr/bin/env python3
"""
Manual / cron refresh of the EUR fx-rate table (Task 4).

Fetches current EUR-base reference rates from **frankfurter.app** (a free,
key-less proxy over the European Central Bank daily reference rates) and writes
them to ``data/fx_rates.json`` atomically. This runs **out of band** — never in
the request path (Global Constraint 4 / docs/UPGRADE-PLAN.md §3): the provider
boundary reads the committed table, and this script keeps it fresh.

Usage:

    .venv/bin/python scripts/refresh_fx.py            # refresh in place
    .venv/bin/python scripts/refresh_fx.py --dry-run  # print, don't write
    .venv/bin/python scripts/refresh_fx.py --symbols HUF,PLN,CZK,RON,GBP,CHF

Cron example (weekly, Mondays 06:00):

    0 6 * * 1  cd /path/to/flight-deals-tracker && .venv/bin/python scripts/refresh_fx.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests

from flight_deals.paths import resolve_path

logger = logging.getLogger("refresh_fx")

# The currencies we care about (Wizz + neighbouring LCC markets). Extra symbols
# already in the seed are preserved unless --symbols overrides the set.
DEFAULT_SYMBOLS = ["HUF", "PLN", "CZK", "RON", "GBP", "CHF", "BGN", "SEK", "NOK", "DKK"]
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
FX_RATES_FILE = "data/fx_rates.json"


def fetch_rates(symbols: list[str]) -> dict:
    params = {"from": "EUR", "to": ",".join(symbols)}
    resp = requests.get(FRANKFURTER_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    rates = data.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise SystemExit(f"refresh_fx: unexpected response from frankfurter.app: {data!r}")
    # frankfurter returns EUR-base rates already (1 EUR = <rate> foreign).
    return {
        "schema_version": 1,
        "base": "EUR",
        "as_of": data.get("date") or datetime.now(timezone.utc).date().isoformat(),
        "source": f"frankfurter.app (ECB) fetched {datetime.now(timezone.utc).isoformat()}",
        "note": "EUR-base rates: 1 EUR = <rate> units of the listed currency. Wizz BUD fares arrive in HUF.",
        "rates": {k.upper(): round(float(v), 6) for k, v in sorted(rates.items())},
    }


def write_atomic(payload: dict) -> None:
    path = resolve_path(FX_RATES_FILE)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description="Refresh data/fx_rates.json from frankfurter.app (ECB).")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="Comma-separated currency codes (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="Print the new table instead of writing it.")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    payload = fetch_rates(symbols)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    write_atomic(payload)
    logger.info("refresh_fx: wrote %d rates to %s (as_of %s)",
                len(payload["rates"]), resolve_path(FX_RATES_FILE), payload["as_of"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
