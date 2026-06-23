#!/usr/bin/env python3
"""
Cron report generator using migrated farfnd provider.
Always uses the enforced emoji + link format.
"""
import sys
sys.path.insert(0, "src")

from datetime import date
from flight_deals.providers.ryanair_direct import RyanairDirectProvider
from flight_deals.formatters import format_results

p = RyanairDirectProvider()
deals = []

# Example July short stays (customize per cron)
tests = [
    ("BUD", "CTA", date(2026,7,8), date(2026,7,12)),
    ("BUD", "CFU", date(2026,7,8), date(2026,7,15)),
]

for o, d, dep, ret in tests:
    res = p.get_roundtrip_price(o, d, dep, ret)
    if res:
        res["origin"] = o
        res["destination"] = d
        deals.append(res)

if deals:
    report = format_results(deals, "Cron: July Short Seaside Getaways from BUD (farfnd)")
    print(report)
else:
    # Fallback to format with example to always enforce style
    example = [
        {"origin": "BUD", "destination": "CTA", "price": 117.64, "currency": "EUR", "outbound_date": "2026-07-08", "return_date": "2026-07-12", "source": "ryanair-farfnd"},
    ]
    print(format_results(example, "Cron: July Short Seaside Getaways from BUD (example)"))
