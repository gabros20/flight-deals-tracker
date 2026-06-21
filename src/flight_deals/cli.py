import typer
from rich.console import Console
from rich.table import Table
from datetime import datetime
from flight_deals.orchestrator import DealOrchestrator
from flight_deals.history import PriceHistoryStore
from flight_deals.notifier import TelegramNotifier
from flight_deals.models import PriceSnapshot
from flight_deals.registry.destinations import DestinationRegistry

app = typer.Typer(
    name="flight-deals",
    help="Flight Deals Tracker - Find and track Ryanair & Wizz Air deals",
    add_completion=False,
)
console = Console()
orchestrator = DealOrchestrator()
history_store = PriceHistoryStore()
notifier = TelegramNotifier()
registry = DestinationRegistry()


@app.command()
def search(
    category: str = typer.Option(..., "--category", "-c"),
    origin: str = typer.Option(..., "--from", "-f"),
    date_from: str = typer.Option(..., "--date-from"),
    date_to: str = typer.Option(..., "--date-to"),
    return_from: str = typer.Option(None, "--return-from"),
    return_to: str = typer.Option(None, "--return-to"),
    max_price: int = typer.Option(None, "--max-price"),
):
    """Search deals by category"""
    deals = orchestrator.search_by_category(
        category=category,
        origin=origin,
        date_from=date_from,
        date_to=date_to,
        max_price=max_price,
        return_date_from=return_from,
        return_date_to=return_to,
    )
    if not deals:
        console.print("[yellow]No deals found[/yellow]")
        return

    table = Table(title=f"Deals for {category}")
    table.add_column("Route", style="cyan")
    table.add_column("Date", style="green")
    table.add_column("Price", style="magenta")
    table.add_column("Source", style="yellow")

    for deal in deals[:20]:
        route = f"{deal.origin} → {deal.destination}"
        table.add_row(route, deal.departure_date, f"{deal.price} {deal.currency}", deal.source)

    console.print(table)


@app.command()
def roundtrip(
    origin: str = typer.Option(..., "--origin", "-o"),
    destination: str = typer.Option(..., "--destination", "-d"),
    outbound_from: str = typer.Option(..., "--outbound-from"),
    outbound_to: str = typer.Option(..., "--outbound-to"),
    return_from: str = typer.Option(..., "--return-from"),
    return_to: str = typer.Option(..., "--return-to"),
    max_price: int = typer.Option(None, "--max-price"),
):
    """Find paired round-trip deals"""
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
    origin: str = typer.Option(..., "--origin", "-o"),
    destination: str = typer.Option(..., "--destination", "-d"),
    date_out: str = typer.Option(..., "--date-out"),
    date_return: str = typer.Option(None, "--date-return"),
    threshold: float = typer.Option(15.0, "--threshold", "-t"),
    currency: str = typer.Option("EUR", "--currency"),
):
    """Track a route with price drop alerts (checks both Ryanair and Wizz)"""
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
            msg = f"PRICE ALERT: {origin}-{destination} {date_out} changed {change:+.1f}%"
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


@app.command()
def version():
    """Show version"""
    typer.echo("Flight Deals Tracker v0.3.1")


if __name__ == "__main__":
    app()
    app()