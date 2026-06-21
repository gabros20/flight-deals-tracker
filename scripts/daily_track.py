"""
Example daily tracking script (can be run via cron or GitHub Actions)
"""
from flight_deals.cli import app
from typer.testing import CliRunner

runner = CliRunner()

def run_daily_tracks():
    # Example routes to track daily
    routes = [
        ("STN", "BGY", "2026-08-20"),
        ("BUD", "ALC", "2026-08-15"),
    ]
    for origin, dest, date in routes:
        result = runner.invoke(app, [
            "track",
            "--origin", origin,
            "--destination", dest,
            "--date-out", date,
            "--threshold", "15"
        ])
        print(result.output)

if __name__ == "__main__":
    run_daily_tracks()