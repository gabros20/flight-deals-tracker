#!/usr/bin/env python3
"""Test round-trip prices using stable farfnd (migrated provider).
Enforces the emoji + link format for all outputs.
"""
from datetime import date
import sys
sys.path.insert(0, "src")

from flight_deals.providers.ryanair_direct import RyanairDirectProvider
from flight_deals.formatters import format_results

p = RyanairDirectProvider()

tests = [
    ("BUD", "CTA", date(2026,7,8), date(2026,7,12)),
    ("BUD", "CFU", date(2026,7,8), date(2026,7,15)),
    ("BUD", "HER", date(2026,7,9), date(2026,7,13)),
]

deals = []
for origin, dest, dep, ret in tests:
    res = p.get_roundtrip_price(origin, dest, dep, ret)
    if res:
        res["origin"] = origin
        res["destination"] = dest
        deals.append(res)
    else:
        print(f"No price for {origin}-{dest}")

if deals:
    report = format_results(deals, "July Short Getaways from BUD (Italy & Greece Seaside) - farfnd")
    print(report)
else:
    print("No real deals returned. Check dates or rate limits.")
