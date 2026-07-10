#!/usr/bin/env python3
"""
Cron report generator using the rebuilt farfnd Ryanair provider (Task 3).
Prints an honest report; never fabricates deals.
"""
import sys
sys.path.insert(0, "src")

from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.http import ProviderError
from flight_deals.formatters import format_results

p = RyanairProvider()
deals = []

# Example short stays (customize per cron)
tests = [
    ("BUD", "CTA", "2026-07-08", "2026-07-12"),
    ("BUD", "CFU", "2026-07-08", "2026-07-15"),
]

for o, d, dep, ret in tests:
    try:
        pairs = p.roundtrip_fares(o, d, out_from=dep, out_to=dep, ret_from=ret, ret_to=ret)
    except ProviderError as e:
        print(f"provider error for {o}->{d}: {e}", file=sys.stderr)
        continue
    for fp in pairs:
        deals.append({
            "origin": fp.origin,
            "destination": fp.destination,
            "price": fp.total_price_eur,
            "currency": "EUR",
            "outbound_date": fp.out_date,
            "return_date": fp.return_date,
            "source": "ryanair-farfnd",
        })

title = "Cron: Short Seaside Getaways from BUD (farfnd)"
if deals:
    print(format_results(deals, title))
else:
    print(format_results([], title))
    print("No live prices returned for the configured routes/dates (provider empty or down).")
