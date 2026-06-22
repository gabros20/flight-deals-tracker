#!/usr/bin/env python
"""
Cron-friendly collection script for flight deals history.
Usage: python scripts/collect_deals.py --category european-islands --date-from 2026-08-01 --date-to 2026-08-15 [--connections]
Can be scheduled via Hermes cron or system crontab.
"""
import sys
import argparse
from pathlib import Path

# Ensure project root in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from flight_deals.cli import app  # reuses typer commands but we call orchestrator directly
from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.config import get_config

def main():
    parser = argparse.ArgumentParser(description="Collect prices into history for cron")
    parser.add_argument("--category", "-c", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--origin", default=None)
    parser.add_argument("--connections", action="store_true")
    parser.add_argument("--window", type=int, default=None, help="History window")
    args = parser.parse_args()

    config = get_config()
    origin = args.origin or config.default_origin
    orchestrator = DealOrchestrator()
    history = PriceHistoryStore()

    print(f"[collect] Starting for {args.category} from {origin} {args.date_from}..{args.date_to}")
    deals = orchestrator.search_by_category(
        category=args.category,
        origin=origin,
        date_from=args.date_from,
        date_to=args.date_to,
        connections=args.connections,
        history_window_days=args.window,
    )
    count = 0
    for deal in deals:
        try:
            history.append_from_deal(deal)
            count += 1
        except Exception as e:
            print(f"  skip: {e}")
    print(f"[collect] Logged {count} snapshots.")

    # Auto detect drops
    try:
        alerts = history.detect_price_drops(deals)
        if alerts:
            print(f"[collect] ALERTS: {len(alerts)} drops below avg")
            for a in alerts:
                print("  ", a["message"])
    except Exception as e:
        print(f"[collect] Alert check: {e}")

    print("[collect] Done. Data is file-based CSV.")

if __name__ == "__main__":
    main()
