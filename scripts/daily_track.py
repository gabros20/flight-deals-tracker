"""
Daily tracking script for **local** cron or scheduled runs on your own machine only.

Everything runs locally — no GitHub Actions or cloud services.


Usage examples:
  python scripts/daily_track.py
  python scripts/daily_track.py --routes "BUD-CAG:2026-08-12, STN-BGY:2026-08-20"

It uses the project's config system for defaults and Telegram.
"""

import typer
from flight_deals.cli import app as flight_app
from typer.testing import CliRunner
from flight_deals.config import get_config

runner = CliRunner()
config = get_config()


def track_route(origin: str, destination: str, date_out: str, threshold: float = 12.0):
    """Run a single track via the CLI"""
    args = [
        "track",
        "--origin", origin,
        "--destination", destination,
        "--date-out", date_out,
        "--threshold", str(threshold),
    ]
    result = runner.invoke(flight_app, args)
    return result.output


def run_daily_tracks(routes=None):
    """Main function to run tracking for a list of routes"""
    if routes is None:
        # Default routes - customize these
        routes = [
            ("BUD", "CAG", "2026-08-12", 10.0),   # Italian gem
            ("BUD", "PMI", "2026-08-15", 15.0),   # Island
            ("STN", "BGY", "2026-08-20", 12.0),   # Popular route
            ("BUD", "CFU", "2026-08-10", 15.0),
        ]

    print(f"Running daily flight deal tracking (origin default: {config.default_origin})")
    print(f"Telegram configured: {bool(config.telegram_bot_token and config.telegram_chat_id)}\n")

    for item in routes:
        if len(item) == 4:
            origin, dest, date, threshold = item
        else:
            origin, dest, date = item
            threshold = 12.0

        print(f"Tracking {origin} → {dest} on {date} (threshold {threshold}%)")
        output = track_route(origin, dest, date, threshold)
        print(output)
        print("-" * 50)


if __name__ == "__main__":
    # Allow running with custom routes via command line if needed
    import sys
    if len(sys.argv) > 1:
        # Simple parsing for --routes "BUD-CAG:2026-08-12,STN-BGY:2026-08-20"
        routes_arg = sys.argv[1] if sys.argv[1].startswith("--routes") else None
        if routes_arg:
            raw = routes_arg.split("=", 1)[1]
            parsed = []
            for r in raw.split(","):
                parts = r.strip().split(":")
                if len(parts) == 2:
                    o, d = parts[0].split("-")
                    parsed.append((o, d, parts[1]))
            run_daily_tracks(parsed)
        else:
            run_daily_tracks()
    else:
        run_daily_tracks()