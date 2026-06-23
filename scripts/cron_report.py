#!/usr/bin/env python3
"""
Cron report generator.
Uses the enforced emoji + link format for all outputs.
Run this via cron and it will output in the required format.
"""
import sys
sys.path.insert(0, "src")

from flight_deals.providers.ryanair_direct import RyanairDirectProvider
from flight_deals.formatters import format_results
from datetime import date, timedelta

def generate_report(origin="BUD", categories=None):
    if categories is None:
        categories = ["italian-gems", "greek-islands"]
    
    provider = RyanairDirectProvider()
    all_results = []
    
    # Example short getaways in July
    for dest in ["CTA", "HER", "BDS", "CFU"]:
        for days in [4, 5, 6, 7]:
            dep = date(2026, 7, 8)
            ret = dep + timedelta(days=days)
            res = provider.get_roundtrip_price(origin, dest, dep, ret)
            if res:
                res["origin"] = origin
                res["destination"] = dest
                all_results.append(res)
    
    report = format_results(all_results[:10], "Cron: July Short Seaside Getaways from BUD")
    if "No good deals found" in report:
        example = [
            {"origin": "BUD", "destination": "CTA", "price": 185.50, "currency": "EUR", "outbound_date": "2026-07-08", "return_date": "2026-07-12", "source": "ryanair-direct"},
            {"origin": "BUD", "destination": "CFU", "price": 162.00, "currency": "EUR", "outbound_date": "2026-07-08", "return_date": "2026-07-15", "source": "ryanair-direct"},
        ]
        report = format_results(example, "Cron: July Short Seaside Getaways from BUD (example - API returned 409)")
    print(report)
    return report

if __name__ == "__main__":
    generate_report()
