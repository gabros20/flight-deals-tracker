import typer
from rich.console import Console
from rich.table import Table
from datetime import datetime
from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.notifier import TelegramNotifier
from flight_deals.models import PriceSnapshot
from flight_deals.registry.destinations import DestinationRegistry
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
):
    """Search deals by category (uses reachability + cache). Use --connections for 1-stop options."""
    origin = origin or config.default_origin
    deals = orchestrator.search_by_category(
        category=category,
        origin=origin,
        date_from=date_from,
        date_to=date_to,
        max_price=max_price,
        return_date_from=return_from,
        return_date_to=return_to,
        connections=connections,
    )
    if not deals:
        console.print("[yellow]No deals found[/yellow]")
        return

    title = f"Deals for {category} from {origin}"
    if connections:
        title += " (incl. 1-stop options)"
    table = Table(title=title)
    table.add_column("Route", style="cyan")
    table.add_column("Date", style="green")
    table.add_column("Price", style="magenta")
    table.add_column("Source", style="yellow")
    table.add_column("Stops", style="dim")

    for deal in deals[:25]:
        route = f"{deal.origin} → {deal.destination}"
        stops_str = str(getattr(deal, "stops", 0)) if getattr(deal, "stops", 0) > 0 else "direct"
        table.add_row(route, deal.departure_date, f"{deal.price} {deal.currency}", deal.source, stops_str)

    console.print(table)
    note = f"Showing top {min(25, len(deals))} of {len(deals)} deals (cached where possible)"
    if connections:
        note += " | --connections includes popular 1-stop via major hubs (VIE, MUC, etc.)"
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

    table = Table(title="Roundtrip Deals")
    table.add_column("Outbound", style="cyan")
    table.add_column("Return", style="green")
    table.add_column("Total", style="magenta")

    for out, ret in pairs:
        total = out.price + ret.price
        table.add_row(
            f"{out.departure_date} {out.price}{out.currency}",
            f"{ret.departure_date} {ret.price}{ret.currency}",
            f"{total}{out.currency}"
        )
    console.print(table)


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
    table = Table(title="Price History")
    table.add_column("Date", style="green")
    table.add_column("Route", style="cyan")
    table.add_column("Price", style="magenta")
    for s in snapshots:
        table.add_row(s.departure_date, f"{s.origin}-{s.destination}", f"{s.price} {s.currency}")
    console.print(table)


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
        console.print(f"  Config file: {save_user_config(config)}")  # this also saves current state


@app.command()
def version():
    """Show version"""
    typer.echo("Flight Deals Tracker v0.4.0 (config + cache + real Telegram + reachability)")



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
        table = Table(title="Cache Entries")
        table.add_column("File")
        table.add_column("Provider")
        table.add_column("Route")
        table.add_column("Dates")
        table.add_column("Deals")
        for e in entries[:20]:
            table.add_row(
                e.get("file", ""),
                e.get("provider", ""),
                f"{e.get('origin','')}→{e.get('destination','')}",
                f"{e.get('date_from','')}..{e.get('date_to','')}",
                str(e.get("num_deals", 0))
            )
        console.print(table)
    else:
        console.print("[red]Unknown action. Use: clear, stats, list[/red]")


if __name__ == "__main__":
    app()