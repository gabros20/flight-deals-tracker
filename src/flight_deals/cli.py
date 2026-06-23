import typer
from rich.console import Console
from rich.table import Table
from datetime import datetime
from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.notifier import TelegramNotifier
from flight_deals.models import PriceSnapshot
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.formatters import format_results
from flight_deals.config import get_config, save_user_config, FlightDealsConfig


app = typer.Typer(
    name="flight-deals",
    help="Flight Deals Tracker - Find and track Ryanair & Wizz Air deals",
    add_completion=False,
)
console = Console()

# Initialize with config
config = get_config()
orchestrator = DealOrchestrator()
history_store = PriceHistoryStore(str(config.history_path))
notifier = TelegramNotifier(config=config)
registry = DestinationRegistry()


@app.command()
def search(
    category: str = typer.Option(..., "--category", "-c"),
    origin: str = typer.Option(None, "--from", "-f"),
    date_from: str = typer.Option(..., "--date-from"),
    date_to: str = typer.Option(..., "--date-to"),
    return_from: str = typer.Option(None, "--return-from"),
    return_to: str = typer.Option(None, "--return-to"),
    max_price: int = typer.Option(None, "--max-price"),
    connections: bool = typer.Option(False, "--connections", "--with-stops", help="Include destinations reachable with 1 stop via hubs"),
    max_ground_minutes: int = typer.Option(180, "--max-ground-minutes", help="Filter connections with ground time > this (minutes)"),
    ground_prefer: str = typer.Option("any", "--ground-prefer", help="driving|public|any"),
    sort_by: str = typer.Option("price", "--sort-by", help="price|total-time|efficiency"),
    history_window: int = typer.Option(None, "--history-window", help="Days of history to use for comparisons (default from config)"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache and fetch fresh prices"),
):
    """Search deals by category (uses reachability + cache). Use --connections for 1-stop options."""
    origin = origin or config.default_origin
    deals = orchestrator.search_by_category(
        category=category,
        origin=origin,
        fresh=fresh,
        date_from=date_from,
        date_to=date_to,
        max_price=max_price,
        return_date_from=return_from,
        return_date_to=return_to,
        connections=connections,
        max_ground_minutes=max_ground_minutes,
        ground_prefer=ground_prefer,
        sort_by=sort_by,
        history_window_days=history_window,
    )
    if not deals:
        console.print("[yellow]No deals found[/yellow]")
        return

    title = f"Deals for {category} from {origin}"
    if connections:
        title += " (incl. 1-stop + multi-airport self-transfers (Milan BGY-MXP, Istanbul IST-SAW, London))"
    # TABLE REMOVED - enforced emoji + link format via formatters.py for all outputs including cron
    # Enforce shared formatter (emoji + links) for CLI + cron
    deal_dicts = []
    for deal in deals[:25]:
        d = {
            "origin": getattr(deal, "origin", ""),
            "destination": getattr(deal, "destination", ""),
            "price": getattr(deal, "price", 0),
            "currency": getattr(deal, "currency", "EUR"),
            "outbound_date": getattr(deal, "departure_date", ""),
            "return_date": getattr(deal, "return_date", return_to or ""),
            "source": getattr(deal, "source", ""),
        }
        deal_dicts.append(d)
    formatted = format_results(deal_dicts, title)
    console.print(formatted)
    note = f"Showing top {min(25, len(deals))} of {len(deals)} deals"
    console.print(f"[dim]{note}[/dim]")
    if hasattr(orchestrator, "apify") and orchestrator.apify.is_available:
        console.print("[yellow]Note: Apify multi-source used (~/bin/bash.0003/search). Results may include self-transfers.[/yellow]")


@app.command()
def roundtrip(
    origin: str = typer.Option(None, "--origin", "-o"),
    destination: str = typer.Option(..., "--destination", "-d"),
    outbound_from: str = typer.Option(..., "--outbound-from"),
    outbound_to: str = typer.Option(..., "--outbound-to"),
    return_from: str = typer.Option(..., "--return-from"),
    return_to: str = typer.Option(..., "--return-to"),
    max_price: int = typer.Option(None, "--max-price"),
):
    """Find paired round-trip deals"""
    origin = origin or config.default_origin
    pairs = orchestrator.find_roundtrip_deals(
        origin, destination, outbound_from, outbound_to, return_from, return_to, max_price
    )
    if not pairs:
        console.print("[yellow]No roundtrips found[/yellow]")
        return

    # Use enforced formatter instead of table
    deal_dicts = []
    for out, ret in pairs:
        total = getattr(out, 'price', 0) + getattr(ret, 'price', 0)
        d = {
            "origin": origin,
            "destination": destination,
            "price": total,
            "currency": getattr(out, 'currency', 'EUR'),
            "outbound_date": getattr(out, 'departure_date', ''),
            "return_date": getattr(ret, 'departure_date', ''),
            "source": 'roundtrip',
        }
        deal_dicts.append(d)
    formatted = format_results(deal_dicts, f'Roundtrip Deals {origin}→{destination}')
    console.print(formatted)


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

    # Try both providers
    deals = orchestrator.ryanair.get_cheapest_flights(origin, date_out, date_out, destination)
    if not deals:
        deals = orchestrator.wizz.get_cheapest_flights(origin, date_out, date_out, destination)

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
            notifier.send_deal(msg)
        else:
            console.print(f"Current: {current.price} {current.currency} (prev {previous}, {change:+.1f}%)")
    else:
        console.print(f"Current price: {current.price} {current.currency} (first tracking)")

    snapshot = PriceSnapshot(
        timestamp_utc=datetime.utcnow(),
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


@app.command()
def history(
    origin: str = typer.Option(None, "--origin"),
    destination: str = typer.Option(None, "--destination"),
    limit: int = typer.Option(10, "--limit"),
):
    """Show price history for a route"""
    snapshots = history_store.get_history(origin, destination, limit)
    if not snapshots:
        console.print("[yellow]No history found[/yellow]")
        return
    for s in snapshots:
        console.print(f"{s.date}: {s.price} {s.currency}")


@app.command("config")
def config_cmd(
    show: bool = typer.Option(True, "--show"),
    set_origin: str = typer.Option(None, "--set-default-origin"),
    set_token: str = typer.Option(None, "--set-telegram-token"),
    set_chat: str = typer.Option(None, "--set-telegram-chat"),
):
    """View or update configuration"""
    if set_origin:
        config.default_origin = set_origin
        save_user_config(config)
        console.print(f"[green]Default origin set to {set_origin}[/green]")

    if set_token:
        config.telegram_bot_token = set_token
        save_user_config(config)
        console.print("[green]Telegram token saved[/green]")

    if set_chat:
        config.telegram_chat_id = set_chat
        save_user_config(config)
        console.print("[green]Telegram chat ID saved[/green]")

    if show:
        console.print("Current configuration:")
        console.print(f"  Default origin: {config.default_origin}")
        console.print(f"  Currency: {config.currency}")
        console.print(f"  Telegram configured: {bool(config.telegram_bot_token and config.telegram_chat_id)}")
        console.print(f"  Cache TTL: {config.cache_ttl_hours}h")
        console.print(f"  Max workers: {config.max_workers}")
        save_user_config(config)
    console.print("  Config file: updated")


@app.command()
def version():
    """Show version"""
    typer.echo("Flight Deals Tracker v0.6.0 (robust date windows + file-based CSV + cron-ready + price-drop alerts below avg)")



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
def collect(
    category: str = typer.Option(..., "--category", "-c"),
    origin: str = typer.Option(None, "--from", "-f"),
    date_from: str = typer.Option(..., "--date-from"),
    date_to: str = typer.Option(..., "--date-to"),
    connections: bool = typer.Option(False, "--connections"),
):
    """Collect current prices for a category into history (for future comparisons)."""
    origin = origin or config.default_origin
    console.print(f"[cyan]Collecting deals for {category} from {origin}...[/cyan]")
    deals = orchestrator.search_by_category(
        category=category,
        origin=origin,
        fresh=fresh,
        date_from=date_from,
        date_to=date_to,
        connections=connections,
    )
    count = 0
    for deal in deals:
        try:
            history_store.append_from_deal(deal)
            count += 1
        except Exception:
            pass
    console.print(f"[green]Logged {count} price snapshots to history[/green]")

    # Price-drop alerts below historical avg (file-based + Telegram ready)
    try:
        alerts = history_store.detect_price_drops(deals)
        if alerts:
            console.print(f"[bold red]🚨 {len(alerts)} price drops below historical avg detected![/bold red]")
            for a in alerts[:3]:
                console.print("   " + str(a.get("message", a)))
                if hasattr(notifier, "send_price_alert"):
                    notifier.send_price_alert(a["origin"], a["destination"], a["departure_date"], a["current_price"], "EUR", -a.get("pct_below_avg", 0))
        else:
            console.print("[dim]No significant drops vs historical avg.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Drop detection note: {e}[/yellow]")
    console.print("Use 'flight-deals search' or 'flight-deals alerts' to view comparisons/badges.")


@app.command()
def alerts(
    origin: str = typer.Option(None, "--origin"),
    limit: int = typer.Option(20, "--limit"),
):
    """Show logged price-drop alerts (below historical avg)."""
    alerts_path = config.data_path / "price_alerts.csv"
    if not alerts_path.exists():
        console.print("[yellow]No alerts file yet. Run collect first.[/yellow]")
        return
    import csv
    rows = []
    with open(alerts_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)[-limit:]
    if not rows:
        console.print("[yellow]No alerts logged yet.[/yellow]")
        return
    for r in rows:
                      console.print(f"  {r.get(current_price)} {r.get(pct_below_avg)}% below avg")
    pass  # table removed

@app.command("history-stats")
def history_stats(
    origin: str = typer.Option(None, "--origin"),
    destination: str = typer.Option(None, "--destination"),
    window: int = typer.Option(None, "--window", help="History window in days for robust filtering"),
):
    """Show aggregate historical stats for a route (supports --window for date filtering)."""
    stats = history_store.get_route_stats(origin or config.default_origin, destination or "", window_days=window)
    if not stats or stats.get("count", 0) == 0:
        console.print("[yellow]No history for this route yet. Use 'collect' first.[/yellow]")
        return
    w = stats.get("window_days_used", "default")
    console.print(f"[bold]History Stats for {origin or config.default_origin} → {destination or 'any'} (window={w}d)[/bold]")
    for k, v in stats.items():
        console.print(f"  {k}: {v}")

if __name__ == "__main__":
    app()



@app.command()
def multi_airports():
    """List multi-airport self-transfer hubs supported for --connections."""
    reg = DestinationRegistry()
    cities = reg.get_multi_airport_cities()
    console.print("[bold]Supported Multi-Airport Self-Transfer Hubs[/bold]")
    for city in cities:
        airports = reg.get_airports_for_multi_city(city)
        console.print(f"  {city}: {', '.join(airports)}")
    console.print("\nUse with: flight-deals search --connections ...")


if __name__ == "__main__":
    app()
@app.command()
def alerts(
    origin: str = typer.Option(None, "--origin"),
    limit: int = typer.Option(20, "--limit"),
):
    """Show logged price-drop alerts (below historical avg). Run collect to populate."""
    alerts_path = config.data_path / "price_alerts.csv"
    if not alerts_path.exists():
        console.print("[yellow]No alerts file yet. Run collect first to generate data.[/yellow]")
        return
    import csv
    rows = []
    with open(alerts_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)[-limit:]
    if not rows:
        console.print("[yellow]No alerts logged yet.[/yellow]")
        return
    for r in rows:
                      console.print(f"  {r.get(current_price)} {r.get(pct_below_avg)}% below avg")
    pass  # table removed
