"""
Hermes Skill Wrapper for Flight Deals Tracker
Allows natural language use via the Hermes agent.
"""

from flight_deals.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def search_deals(category: str, origin: str, date_from: str, date_to: str, max_price: int = None, return_date_from: str = None, return_date_to: str = None):
    """Search for deals by category (called by Hermes)"""
    args = [
        "search",
        "--category", category,
        "--from", origin,
        "--date-from", date_from,
        "--date-to", date_to,
    ]
    if max_price:
        args += ["--max-price", str(max_price)]
    if return_date_from:
        args += ["--return-from", return_date_from]
    if return_date_to:
        args += ["--return-to", return_date_to]

    result = runner.invoke(app, args)
    return result.output


def track_route(origin: str, destination: str, date_out: str, threshold: float = 15.0, date_return: str = None):
    """Track a route (called by Hermes)"""
    args = [
        "track",
        "--origin", origin,
        "--destination", destination,
        "--date-out", date_out,
        "--threshold", str(threshold),
    ]
    if date_return:
        args += ["--date-return", date_return]

    result = runner.invoke(app, args)
    return result.output


def find_roundtrip(origin: str, destination: str, outbound_from: str, outbound_to: str, return_from: str, return_to: str, max_price: int = None):
    """Find roundtrip deals (called by Hermes)"""
    args = [
        "roundtrip",
        "--origin", origin,
        "--destination", destination,
        "--outbound-from", outbound_from,
        "--outbound-to", outbound_to,
        "--return-from", return_from,
        "--return-to", return_to,
    ]
    if max_price:
        args += ["--max-price", str(max_price)]

    result = runner.invoke(app, args)
    return result.output


def list_destinations(tag: str = None):
    """List destinations (called by Hermes)"""
    args = ["destinations"]
    if tag:
        args += ["--tag", tag]
    result = runner.invoke(app, args)
    return result.output


def show_history(origin: str = None, destination: str = None, limit: int = 10):
    """Show price history (called by Hermes)"""
    args = ["history", "--limit", str(limit)]
    if origin:
        args += ["--origin", origin]
    if destination:
        args += ["--destination", destination]
    result = runner.invoke(app, args)
    return result.output