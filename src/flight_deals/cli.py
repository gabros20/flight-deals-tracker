import json
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console

from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.notifier import TelegramNotifier
from flight_deals.models import PriceSnapshot
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.formatters import format_results
from flight_deals.config import get_config, save_user_config


app = typer.Typer(
    name="flight-deals",
    help="Flight Deals Tracker - Find and track Ryanair & Wizz Air deals",
    add_completion=False,
)
console = Console()

# Config, history store, and the destination registry are plain file/env
# reads (no network) and safe to build at import time. The orchestrator and
# notifier are NOT — DealOrchestrator() spins up provider clients that hit
# the network (e.g. WizzProvider's version sniff), so they must stay lazy or
# `flight-deals --help` would make network calls. See docs/UPGRADE-PLAN.md
# Phase 0 requirement 6.
config = get_config()
history_store = PriceHistoryStore(str(config.history_path))
registry = DestinationRegistry()

_orchestrator: Optional[DealOrchestrator] = None
_notifier: Optional[TelegramNotifier] = None


def get_orchestrator() -> DealOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DealOrchestrator()
    return _orchestrator


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier(config=config)
    return _notifier


def _removed_pending_rebuild() -> None:
    """Print the standard stub error for surface removed in Phase 0, pending rebuild."""
    typer.echo(json.dumps({"error": "removed_pending_rebuild", "hint": "see docs/UPGRADE-PLAN.md"}))


def _print_sources(orch: DealOrchestrator) -> None:
    """Per-provider health for the last search, so failures are visible instead of silent."""
    status = orch.provider_status
    if not status:
        return
    parts = [f"{name}={st.get('status', 'ok' if st.get('ok') else 'error')}" for name, st in sorted(status.items())]
    console.print(f"[dim]sources: {', '.join(parts)}[/dim]")


@app.command()
def search(
    category: str = typer.Option(..., "--category", "-c"),
    origin: str = typer.Option(None, "--from", "-f"),
    date_from: str = typer.Option(..., "--date-from"),
    date_to: str = typer.Option(..., "--date-to"),
    return_from: str = typer.Option(None, "--return-from"),
    return_to: str = typer.Option(None, "--return-to"),
    max_price: int = typer.Option(None, "--max-price"),
    connections: bool = typer.Option(
        False, "--connections", "--with-stops",
        help="Removed pending rebuild (see docs/UPGRADE-PLAN.md); flag is accepted but errors.",
    ),
    sort_by: str = typer.Option("price", "--sort-by", help="price|total-time|efficiency"),
    history_window: int = typer.Option(None, "--history-window", help="Days of history to use for comparisons (default from config)"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache and fetch fresh prices"),
):
    """Search one-way deals by category. Round-trip (--return-from/--return-to)
    and --connections are removed pending rebuild — see docs/UPGRADE-PLAN.md."""
    if return_from or return_to or connections:
        _removed_pending_rebuild()
        raise typer.Exit(2)

    origin = origin or config.default_origin
    orch = get_orchestrator()
    deals = orch.search_by_category(
        category=category,
        origin=origin,
        fresh=fresh,
        date_from=date_from,
        date_to=date_to,
        max_price=max_price,
        sort_by=sort_by,
        history_window_days=history_window,
    )
    if not deals:
        console.print("[yellow]No deals found[/yellow]")
        _print_sources(orch)
        return

    title = f"Deals for {category} from {origin}"
    deal_dicts = []
    for deal in deals[:25]:
        d = {
            "origin": getattr(deal, "origin", ""),
            "destination": getattr(deal, "destination", ""),
            "price": getattr(deal, "price", 0),
            "currency": getattr(deal, "currency", "EUR"),
            "outbound_date": getattr(deal, "departure_date", ""),
            "return_date": getattr(deal, "return_date", "") or "",
            "source": getattr(deal, "source", ""),
        }
        deal_dicts.append(d)
    formatted = format_results(deal_dicts, title)
    console.print(formatted)
    note = f"Showing top {min(25, len(deals))} of {len(deals)} deals"
    console.print(f"[dim]{note}[/dim]")
    _print_sources(orch)


@app.command()
def roundtrip():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md (Phase 1)."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


@app.command()
def track(
    origin: str = typer.Option(None, "--origin", "-o"),
    destination: str = typer.Option(..., "--destination", "-d"),
    date_out: str = typer.Option(..., "--date-out"),
    date_return: str = typer.Option(None, "--date-return"),
    threshold: float = typer.Option(15.0, "--threshold", "-t"),
):
    """Track a route with price drop alerts (real Telegram if configured)"""
    origin = origin or config.default_origin
    orch = get_orchestrator()

    # Try both providers; typed provider failures must not crash `track`.
    from flight_deals.http import ProviderError

    deals = []
    try:
        deals = orch.ryanair.get_cheapest_flights(origin, date_out, date_out, destination)
    except (ProviderError, Exception) as e:
        console.print(f"[dim]ryanair unavailable: {e}[/dim]")
    if not deals:
        try:
            deals = orch.wizz.get_cheapest_flights(origin, date_out, date_out, destination) or []
        except Exception as e:
            console.print(f"[dim]wizz unavailable: {e}[/dim]")

    if not deals:
        console.print("[red]No current price found for this route[/red]")
        return

    current = deals[0]
    previous = history_store.get_previous_price(origin, destination, date_out)

    if previous:
        change = ((current.price - previous) / previous) * 100
        if abs(change) >= threshold:
            msg = f"PRICE ALERT: {origin}-{destination} {date_out} changed {change:+.1f}% → {current.price} {current.currency}"
            console.print(f"[bold red]{msg}[/bold red]")
            get_notifier().send_deal(msg)
        else:
            console.print(f"Current: {current.price} {current.currency} (prev {previous}, {change:+.1f}%)")
    else:
        console.print(f"Current price: {current.price} {current.currency} (first tracking)")

    snapshot = PriceSnapshot(
        timestamp_utc=datetime.now(timezone.utc),
        origin=origin,
        destination=destination,
        departure_date=date_out,
        return_date=date_return,
        price=current.price,
        currency=current.currency,
        source=current.source,
    )
    history_store.append(snapshot)
    console.print("[green]Price logged to history[/green]")


@app.command()
def destinations(tag: str = typer.Option(None, "--tag")):
    """List destinations"""
    airports = registry.get_by_tag(tag) if tag else registry.airports
    for a in airports:
        console.print(f"{a.iata} - {a.city} ({', '.join(a.tags)})")


where_app = typer.Typer(help="Inspect the tag taxonomy and the --where algebra.")
app.add_typer(where_app, name="where")


@where_app.command("list")
def where_list(pretty: bool = typer.Option(False, "--pretty")):
    """List tags (with counts), aliases, and auto-derived tags."""
    data = registry.where_list()
    if not pretty:
        typer.echo(json.dumps(data))
        return
    console.print("[bold]Tags[/bold] (tag: airports)")
    for tag, count in data["tags"].items():
        console.print(f"  {tag}: {count}")
    console.print("\n[bold]Aliases[/bold] (name -> expression)")
    for name, expansion in data["aliases"].items():
        console.print(f"  {name} -> {expansion}")
    console.print("\n[bold]Derived[/bold] (auto, from route data): " + ", ".join(data["derived"]))


@where_app.command("show")
def where_show(
    expr: str = typer.Argument(..., help='A tag expression, e.g. "seaside & (italy | spain)"'),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Print the airports matching a where-expression.

    Unknown/misspelled tags (e.g. a typo like ``seasid``) never fail
    silently: the response gains ``unknown_tags`` and a ``hint`` with the
    nearest known tag(s). Case is not "unknown" — tags are matched
    case-insensitively. If every identifier in the expression is unknown,
    this exits 2 (nothing could possibly have matched); if only some are
    unknown, it exits 0 with whatever results the known parts produced.
    """
    import difflib

    from flight_deals.registry.where import WhereParseError, extract_identifiers

    try:
        matches = registry.matching(expr)
    except WhereParseError as e:
        typer.echo(json.dumps({"error": str(e), "hint": e.hint}))
        raise typer.Exit(2)

    all_idents = extract_identifiers(expr)
    unknown = registry.unknown_tags(expr)
    hint = None
    if unknown:
        known_universe = sorted(registry.known_tag_universe())
        suggestions = []
        for tag in unknown:
            close = difflib.get_close_matches(tag, known_universe, n=1, cutoff=0.6)
            suggestions.append(f"{tag!r} - did you mean: {close[0]}?" if close else f"{tag!r} is unknown")
        hint = "; ".join(suggestions)

    if unknown and all_idents and set(unknown) == set(all_idents):
        typer.echo(json.dumps({
            "error": f"unknown tag(s): {', '.join(unknown)}",
            "unknown_tags": unknown,
            "hint": hint,
        }))
        raise typer.Exit(2)

    airports = [
        {"iata": a.iata, "city": a.city, "country": a.country, "tags": a.tags}
        for a in matches
    ]
    payload = {"expr": expr, "count": len(airports), "airports": airports}
    if unknown:
        payload["unknown_tags"] = unknown
        payload["hint"] = hint
    if not pretty:
        typer.echo(json.dumps(payload))
        return
    console.print(f"[bold]{len(airports)}[/bold] airports match [cyan]{expr}[/cyan]")
    for a in airports:
        console.print(f"  {a['iata']} - {a['city']}, {a['country']} ({', '.join(a['tags'])})")
    if unknown:
        console.print(f"[yellow]unknown tags: {', '.join(unknown)}[/yellow] ({hint})")


@app.command()
def history():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md. Use 'history-stats' for aggregate stats."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


@app.command("config")
def config_cmd(
    show: bool = typer.Option(True, "--show"),
    set_origin: str = typer.Option(None, "--set-default-origin"),
):
    """View or update configuration. Telegram/Apify secrets are env-only
    (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APIFY_TOKEN) and are never written
    to the config file."""
    if set_origin:
        config.default_origin = set_origin
        save_user_config(config)
        console.print(f"[green]Default origin set to {set_origin}[/green]")

    if show:
        console.print("Current configuration:")
        console.print(f"  Default origin: {config.default_origin}")
        console.print(f"  Currency: {config.currency}")
        console.print(f"  Telegram configured: {bool(config.telegram_bot_token and config.telegram_chat_id)}")
        console.print(f"  Cache TTL: {config.cache_ttl_hours}h")
        console.print(f"  Max workers: {config.max_workers}")
        console.print("  Telegram/Apify secrets are env-only: set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APIFY_TOKEN in your environment.")


@app.command()
def version():
    """Show version"""
    typer.echo("Flight Deals Tracker v0.7.0 (Phase 0: honest output, no fabricated data)")


@app.command()
def cache(
    action: str = typer.Argument(..., help="clear | stats | list"),
    provider: str = typer.Option(None, "--provider"),
    origin: str = typer.Option(None, "--origin"),
    older_than: int = typer.Option(None, "--older-than", help="Hours"),
):
    """Manage the flight search cache"""
    from flight_deals.cache import FlightCache
    c = FlightCache()

    if action == "clear":
        count = c.clear()
        console.print(f"[green]Cleared {count} cache entries[/green]")
    elif action == "stats":
        stats = c.stats()
        console.print("Cache Statistics:")
        for k, v in stats.items():
            console.print(f"  {k}: {v}")
    elif action == "list":
        entries = c.list_entries()
        if not entries:
            console.print("[yellow]Cache is empty[/yellow]")
            return
        for e in entries[:20]:
            console.print(f"  {e.get('origin','?')} → {e.get('destination','?')} {e.get('price', '?')} {e.get('currency','EUR')}")
    else:
        console.print("[red]Unknown action. Use: clear, stats, list[/red]")


@app.command()
def collect():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


@app.command()
def alerts():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


@app.command("history-stats")
def history_stats(
    origin: str = typer.Option(None, "--origin"),
    destination: str = typer.Option(None, "--destination"),
    window: int = typer.Option(None, "--window", help="History window in days for robust filtering"),
):
    """Show aggregate historical stats for a route (supports --window for date filtering)."""
    stats = history_store.get_route_stats(origin or config.default_origin, destination or "", window_days=window)
    if not stats or stats.get("count", 0) == 0:
        console.print("[yellow]No history for this route yet.[/yellow]")
        return
    w = stats.get("window_days_used", "default")
    console.print(f"[bold]History Stats for {origin or config.default_origin} → {destination or 'any'} (window={w}d)[/bold]")
    for k, v in stats.items():
        console.print(f"  {k}: {v}")


@app.command()
def multi_airports():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
