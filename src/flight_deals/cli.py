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
        _notifier = TelegramNotifier()
    return _notifier


def _removed_pending_rebuild() -> None:
    """Print the standard stub error for surface removed in Phase 0, pending rebuild."""
    typer.echo(json.dumps({"error": "removed_pending_rebuild", "hint": "see docs/UPGRADE-PLAN.md"}))


@app.command()
def search(
    category: str = typer.Option(..., "--category", "-c", help='Where-expression, e.g. "seaside" or "italy | spain" (maps onto --where)'),
    origin: str = typer.Option(None, "--from", "-f"),
    date_from: str = typer.Option(..., "--date-from"),
    date_to: str = typer.Option(..., "--date-to"),
    return_from: str = typer.Option(None, "--return-from"),
    return_to: str = typer.Option(None, "--return-to"),
    max_price: float = typer.Option(None, "--max-price"),
    connections: bool = typer.Option(
        False, "--connections", "--with-stops",
        help="Removed pending rebuild (see docs/UPGRADE-PLAN.md); flag is accepted but errors.",
    ),
    sort_by: str = typer.Option("price", "--sort-by", help="Accepted for backward compatibility; ignored — oneway sorts by price then confidence."),
    history_window: int = typer.Option(None, "--history-window", help="Accepted for backward compatibility; ignored — history window comes from config."),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache and fetch fresh prices"),
    max_calls: int = typer.Option(40, "--max-calls"),
    max_results: int = typer.Option(10, "--max-results"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """[DEPRECATED] Search one-way deals by category; kept for backward
    compatibility — use `oneway`/`getaway` instead. A TRUE alias of `oneway`:
    --category becomes the --where expression, --from/--date-from/--date-to/
    --max-price map onto origins/depart/budget, --fresh passes through, and
    the result is the standard JSON envelope (CONTRACT §1). --sort-by and
    --history-window are accepted but ignored (the oneway/intents pipeline
    sorts by price then confidence, and its history window comes from
    config). --connections and --return-from/--return-to remain removed
    pending the round-trip rebuild — see docs/UPGRADE-PLAN.md."""
    if return_from or return_to or connections:
        _removed_pending_rebuild()
        raise typer.Exit(2)

    depart = f"{date_from}..{date_to}"
    _run_intent(where=category, depart=depart, nights=None, budget=max_price,
                origins_opt=origin, max_calls=max_calls, fresh=fresh,
                max_results=max_results, pretty=pretty)


@app.command()
def roundtrip():
    """Removed pending rebuild — see docs/UPGRADE-PLAN.md (Phase 1)."""
    _removed_pending_rebuild()
    raise typer.Exit(2)


# --------------------------------------------------------------------------- #
# Spec layer (Task 6): plan (compile only) + run (compile + execute)          #
# --------------------------------------------------------------------------- #
def _load_spec_input(spec_arg: str) -> dict:
    """Resolve ``--spec`` to a raw dict. Accepts inline JSON (``{...}``), a file
    path (JSON or YAML), or ``-`` for stdin. YAML is a JSON superset so one
    loader reads both; a top-level ``spec:`` wrapper is unwrapped by
    :func:`engine.spec.parse_spec`."""
    import sys

    import yaml

    text = spec_arg
    stripped = spec_arg.strip()
    if stripped == "-":
        text = sys.stdin.read()
    elif not stripped.startswith("{"):
        from pathlib import Path
        p = Path(spec_arg).expanduser()
        if not p.exists():
            raise ValueError(f"spec file not found: {spec_arg}")
        text = p.read_text()
    return yaml.safe_load(text)


def _emit_spec_error(error: str, hint: str, pretty: bool) -> None:
    from flight_deals import output
    env = output.error_envelope(error, hint)
    typer.echo(output.render(env, pretty=pretty))


@app.command()
def plan(
    spec: str = typer.Option(..., "--spec", help="Spec as inline JSON, a file path, or '-' for stdin"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Compile a search spec into a call plan (CONTRACT §6). No network — prints
    the ordered calls, estimated_calls and estimated_seconds so cost can be
    inspected before running."""
    from flight_deals.engine.planner import PlannerRefusal, compile_plan
    from flight_deals.engine.spec import SpecError, parse_spec
    from flight_deals.registry.where import WhereParseError

    try:
        raw = _load_spec_input(spec)
    except Exception as e:
        _emit_spec_error("bad_spec_input", f"could not read --spec: {e}", pretty)
        raise typer.Exit(2)

    try:
        parsed = parse_spec(raw)
        call_plan = compile_plan(parsed, registry)
    except (SpecError, PlannerRefusal) as e:
        _emit_spec_error(type(e).__name__, e.hint, pretty)
        raise typer.Exit(2)
    except WhereParseError as e:
        _emit_spec_error("invalid_where", e.hint, pretty)
        raise typer.Exit(2)

    plan_dict = call_plan.to_dict()
    if pretty:
        console.print(f"[bold]{plan_dict['estimated_calls']} calls[/bold], "
                      f"~{plan_dict['estimated_seconds']}s (warm-cache, ~1 req/s)")
        for c in plan_dict["calls"]:
            console.print(f"  {c['provider']}/{c['endpoint']} [{c['mode']}] {c['shape']} {c['params']}")
    else:
        typer.echo(json.dumps(plan_dict))


@app.command()
def run(
    spec: str = typer.Option(..., "--spec", help="Spec as inline JSON, a file path, or '-' for stdin"),
    max_calls: int = typer.Option(40, "--max-calls", help="Refuse specs whose plan exceeds this"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache, fetch fresh"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Compile + execute a spec and print the result envelope (CONTRACT §1).
    Exit 0 on success (incl. empty results), 1 on provider failure, 2 on bad
    input."""
    from flight_deals import output
    from flight_deals.engine.planner import Planner, PlannerRefusal
    from flight_deals.engine.spec import SpecError, parse_spec
    from flight_deals.registry.where import WhereParseError

    try:
        raw = _load_spec_input(spec)
    except Exception as e:
        _emit_spec_error("bad_spec_input", f"could not read --spec: {e}", pretty)
        raise typer.Exit(2)

    try:
        parsed = parse_spec(raw)
    except SpecError as e:
        _emit_spec_error("invalid_spec", e.hint, pretty)
        raise typer.Exit(2)

    try:
        planner = Planner(registry=registry)
        env, exit_code = planner.run(parsed, max_calls=max_calls, fresh=fresh)
    except (PlannerRefusal, SpecError) as e:
        _emit_spec_error(type(e).__name__, e.hint, pretty)
        raise typer.Exit(2)
    except WhereParseError as e:
        _emit_spec_error("invalid_where", e.hint, pretty)
        raise typer.Exit(2)

    typer.echo(output.render(env, pretty=pretty))
    if exit_code:
        raise typer.Exit(exit_code)


# --------------------------------------------------------------------------- #
# Intent verbs (Task 7): getaway / oneway / check                             #
# --------------------------------------------------------------------------- #
def _parse_origins(origins_opt: Optional[str]) -> list:
    if not origins_opt:
        return [config.default_origin]
    return [o.strip() for o in origins_opt.split(",") if o.strip()]


def _emit_intent(env: dict, exit_code: int, pretty: bool) -> None:
    from flight_deals import output
    typer.echo(output.render(env, pretty=pretty))
    if exit_code:
        raise typer.Exit(exit_code)


def _run_intent(*, where, depart, nights, budget, origins_opt, max_calls, fresh, max_results, pretty):
    from flight_deals.engine.intents import IntentError, run_search
    from flight_deals.engine.planner import PlannerRefusal
    from flight_deals.engine.spec import SpecError
    from flight_deals.registry.where import WhereParseError

    try:
        env, code = run_search(
            where=where, depart=depart, nights=nights, budget=budget,
            origins=_parse_origins(origins_opt), max_results=max_results,
            max_calls=max_calls, fresh=fresh, registry=registry,
        )
    except (IntentError, SpecError, PlannerRefusal) as e:
        _emit_spec_error(type(e).__name__, e.hint, pretty)
        raise typer.Exit(2)
    except WhereParseError as e:
        _emit_spec_error("invalid_where", e.hint, pretty)
        raise typer.Exit(2)
    _emit_intent(env, code, pretty)


@app.command()
def getaway(
    depart: str = typer.Option(..., "--depart", help="Date, window A..B, month YYYY-MM, or comma list"),
    where: str = typer.Option(None, "--where", help='Tag expression, e.g. "seaside | italy | spain"'),
    nights: str = typer.Option(..., "--nights", help='Nights range for the round-trip, e.g. "5-8"'),
    budget: float = typer.Option(None, "--budget", help="Max total price per person, EUR"),
    origins_opt: str = typer.Option(None, "--from", "--origins", help="Origin IATA(s), comma-separated (default from config)"),
    max_calls: int = typer.Option(40, "--max-calls"),
    max_results: int = typer.Option(10, "--max-results"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache, fetch fresh prices"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Find round-trip getaway deals (S2). Translates intent flags into a spec,
    runs the planner, confirms approximate fares with an exact re-query, enriches
    with price history, and snapshots each deal. JSON envelope on stdout."""
    _run_intent(where=where, depart=depart, nights=nights, budget=budget,
                origins_opt=origins_opt, max_calls=max_calls, fresh=fresh,
                max_results=max_results, pretty=pretty)


@app.command()
def oneway(
    depart: str = typer.Option(..., "--depart", help="Date, window A..B, month YYYY-MM, or comma list"),
    where: str = typer.Option(None, "--where", help='Tag expression, e.g. "seaside | italy | spain"'),
    budget: float = typer.Option(None, "--budget", help="Max price per person, EUR"),
    origins_opt: str = typer.Option(None, "--from", "--origins", help="Origin IATA(s), comma-separated (default from config)"),
    max_calls: int = typer.Option(40, "--max-calls"),
    max_results: int = typer.Option(10, "--max-results"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache, fetch fresh prices"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Find one-way deals (S1). Same builder as `getaway`, without nights.
    (The deprecated-for-agents `search` command aliases this behaviour.)"""
    _run_intent(where=where, depart=depart, nights=None, budget=budget,
                origins_opt=origins_opt, max_calls=max_calls, fresh=fresh,
                max_results=max_results, pretty=pretty)


@app.command()
def check(
    deal_id: str = typer.Argument(..., help="A deal_id from a previous getaway/oneway result"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Re-check a previously seen deal: live exact re-query, delta vs the latest
    and first observation. Unknown id or past dates exit 2 with a hint."""
    from flight_deals.engine.intents import check_deal

    env, code = check_deal(deal_id, registry=registry)
    _emit_intent(env, code, pretty)


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
            get_notifier().send(msg)
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


# --------------------------------------------------------------------------- #
# Saved searches + watches (Task 8): searches add|list|rm|show|due, watch …    #
# --------------------------------------------------------------------------- #
def _month_window(month: Optional[str], months: Optional[str]) -> str:
    """Turn a --month / --months option into a depart DSL string. A single
    month passes through (the DSL understands ``YYYY-MM``); a comma list of
    months widens to a window spanning the first day of the first to the last
    day of the last month."""
    from datetime import date as _date

    def _last_day(ym: str) -> str:
        y, m = (int(x) for x in ym.split("-"))
        nxt = _date(y + 1, 1, 1) if m == 12 else _date(y, m + 1, 1)
        return _date.fromordinal(nxt.toordinal() - 1).isoformat()

    if months:
        items = [m.strip() for m in months.split(",") if m.strip()]
        if not items:
            raise ValueError("--months was empty")
        items.sort()
        return f"{items[0]}-01..{_last_day(items[-1])}"
    if month:
        return month.strip()
    raise ValueError("a watch needs a time window: pass --month YYYY-MM or --months YYYY-MM,YYYY-MM")


def _per_run_calls(spec_dict: dict) -> Optional[int]:
    """Best-effort per-run call estimate for a saved spec (stated on add)."""
    from flight_deals.engine.planner import compile_plan
    from flight_deals.engine.spec import parse_spec as _ps
    try:
        return compile_plan(_ps(spec_dict), registry).estimated_calls
    except Exception:
        return None


def _emit_search_saved(record: dict) -> None:
    from flight_deals.state import searches as _s
    payload = {
        "saved": record["name"],
        "watch": _s.is_watch(record),
        "schedule": record.get("schedule"),
        "alert": record.get("alert"),
        "spec": record["spec"],
        "per_run_calls": _per_run_calls(record["spec"]),
    }
    typer.echo(json.dumps(payload))


searches_app = typer.Typer(help="Saved searches: the specs brief runs on a schedule.")
app.add_typer(searches_app, name="searches")


@searches_app.command("add")
def searches_add(
    spec: str = typer.Option(..., "--spec", help="Spec as inline JSON, a file path, or '-' for stdin"),
    name: str = typer.Option(None, "--name", help="Saved-search name (default: from the spec/file)"),
    schedule: str = typer.Option(None, "--schedule", help='e.g. "daily 08:30", "weekly mon 08:30", "every 6h"'),
    max_price: float = typer.Option(None, "--max-price", help="Attach an alert block (turns this into a watch)"),
    agent_prompt: str = typer.Option(None, "--agent-prompt"),
):
    """Create or replace (idempotent) a saved search from a spec."""
    from flight_deals.state import searches as _s

    try:
        raw = _load_spec_input(spec)
    except Exception as e:
        _emit_spec_error("bad_spec_input", f"could not read --spec: {e}", False)
        raise typer.Exit(2)

    spec_dict = raw.get("spec", raw) if isinstance(raw, dict) else raw
    resolved_name = name or (raw.get("name") if isinstance(raw, dict) else None)
    if not resolved_name:
        _emit_spec_error("missing_name", "pass --name for the saved search, e.g. --name august-seaside", False)
        raise typer.Exit(2)
    alert = {"max_price": max_price, "notify": "telegram"} if max_price is not None else (
        raw.get("alert") if isinstance(raw, dict) else None)
    sched = schedule or (raw.get("schedule") if isinstance(raw, dict) else None)
    prompt = agent_prompt or (raw.get("agent_prompt") if isinstance(raw, dict) else None)

    try:
        record = _s.add(name=resolved_name, spec=spec_dict, schedule=sched, alert=alert, agent_prompt=prompt)
    except _s.SearchError as e:
        _emit_spec_error("invalid_search", e.hint, False)
        raise typer.Exit(2)
    _emit_search_saved(record)


@searches_app.command("list")
def searches_list(pretty: bool = typer.Option(False, "--pretty")):
    """List every saved search."""
    from flight_deals.state import searches as _s
    records = _s.list_all()
    if not pretty:
        typer.echo(json.dumps([
            {"name": r["name"], "schedule": r.get("schedule"), "watch": _s.is_watch(r),
             "alert": r.get("alert")} for r in records
        ]))
        return
    if not records:
        console.print("[yellow]No saved searches yet.[/yellow]")
        return
    for r in records:
        tag = "[watch]" if _s.is_watch(r) else "[search]"
        console.print(f"  {tag} {r['name']}  schedule={r.get('schedule') or '-'}  alert={r.get('alert') or '-'}")


@searches_app.command("show")
def searches_show(name: str = typer.Argument(...), pretty: bool = typer.Option(False, "--pretty")):
    """Show one saved search in full."""
    from flight_deals.state import searches as _s
    record = _s.load(name)
    if record is None:
        typer.echo(json.dumps({"error": "unknown_search", "hint": f"no saved search named {name!r} — run 'flight-deals searches list'"}))
        raise typer.Exit(2)
    if not pretty:
        typer.echo(json.dumps(record))
        return
    console.print_json(json.dumps(record))


@searches_app.command("rm")
def searches_rm(name: str = typer.Argument(...)):
    """Delete a saved search."""
    from flight_deals.state import searches as _s
    existed = _s.remove(name)
    if not existed:
        typer.echo(json.dumps({"error": "unknown_search", "hint": f"no saved search named {name!r}"}))
        raise typer.Exit(2)
    typer.echo(json.dumps({"removed": _s.normalize_name(name)}))


@searches_app.command("due")
def searches_due(pretty: bool = typer.Option(False, "--pretty")):
    """List saved searches currently due to run (by their schedule vs last run)."""
    from flight_deals.state import searches as _s
    now = datetime.now(timezone.utc)
    records = _s.due(now)
    names = [r["name"] for r in records]
    if not pretty:
        typer.echo(json.dumps({"due": names, "count": len(names)}))
        return
    if not names:
        console.print("[dim]Nothing due right now.[/dim]")
        return
    for n in names:
        console.print(f"  {n}")


watch_app = typer.Typer(help="Watches: saved searches with a price-alert threshold.")
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
def watch_add(
    route: str = typer.Argument(None, help="A route like BUD-CFU (route watch)"),
    where: str = typer.Option(None, "--where", "--category", help="A where-expression (category watch)"),
    month: str = typer.Option(None, "--month", help="Watched month YYYY-MM"),
    months: str = typer.Option(None, "--months", help="Comma list of months YYYY-MM,YYYY-MM"),
    nights: str = typer.Option(None, "--nights", help='Nights range, e.g. "4-7" (omit for one-way)'),
    max_price: float = typer.Option(None, "--max-price", help="Alert threshold, EUR (route watch)"),
    budget: float = typer.Option(None, "--budget", help="Alert threshold + search budget, EUR (category watch)"),
    origins_opt: str = typer.Option(None, "--from", "--origins", help="Origin IATA(s) (default from config)"),
    spec_file: str = typer.Option(None, "--spec", help="Build the watch from a spec file/inline JSON instead"),
    name: str = typer.Option(None, "--name", help="Watch name (default: derived from route/where)"),
    schedule: str = typer.Option("daily 08:30", "--schedule", help="Run schedule (default: daily 08:30)"),
):
    """Add a watch = a saved search plus an alert threshold. Three forms:

    \b
      watch add BUD-CFU --months 2026-08,2026-09 --nights 4-7 --max-price 150
      watch add --where "seaside & italy" --month 2026-08 --nights 5-8 --budget 120
      watch add --spec my-search.yaml --name august --max-price 150

    Idempotent: re-adding the same name updates it."""
    from flight_deals.state import searches as _s

    origins = _parse_origins(origins_opt)

    try:
        if spec_file:
            raw = _load_spec_input(spec_file)
            spec_dict = raw.get("spec", raw) if isinstance(raw, dict) else raw
            threshold = max_price if max_price is not None else budget
            if threshold is None:
                threshold = (raw.get("alert") or {}).get("max_price") if isinstance(raw, dict) else None
            if threshold is None:
                raise ValueError("a watch needs an alert threshold: pass --max-price (or --budget)")
            wname = name or (raw.get("name") if isinstance(raw, dict) else None)
            if not wname:
                raise ValueError("pass --name for a --spec watch")
            sched = schedule or (raw.get("schedule") if isinstance(raw, dict) else None)
        elif route:
            if "-" not in route:
                raise ValueError('route must look like BUD-CFU (origin-destination)')
            o, d = [p.strip().upper() for p in route.split("-", 1)]
            depart = _month_window(month, months)
            spec_dict = {"origins": [o], "destinations": [d], "depart": depart}
            if nights:
                spec_dict["nights"] = nights
            if max_price is None:
                raise ValueError("a route watch needs --max-price (the alert threshold)")
            threshold = max_price
            wname = name or f"{o}-{d}".lower()
            sched = schedule
        elif where:
            depart = _month_window(month, months)
            spec_dict = {"origins": origins, "where": where, "depart": depart}
            if nights:
                spec_dict["nights"] = nights
            threshold = budget if budget is not None else max_price
            if threshold is None:
                raise ValueError("a category watch needs --budget (the alert threshold)")
            if budget is not None:
                spec_dict["budget"] = budget
            wname = name or where
            sched = schedule
        else:
            raise ValueError("give a route (BUD-CFU), a --where expression, or a --spec file")

        alert = {"max_price": float(threshold), "notify": "telegram"}
        record = _s.add(name=wname, spec=spec_dict, schedule=sched, alert=alert)
    except (_s.SearchError,) as e:
        _emit_spec_error("invalid_watch", e.hint, False)
        raise typer.Exit(2)
    except ValueError as e:
        _emit_spec_error("invalid_watch", str(e), False)
        raise typer.Exit(2)
    _emit_search_saved(record)


@watch_app.command("list")
def watch_list(pretty: bool = typer.Option(False, "--pretty")):
    """List watches (saved searches carrying an alert block)."""
    from flight_deals.state import searches as _s
    watches = [r for r in _s.list_all() if _s.is_watch(r)]
    if not pretty:
        typer.echo(json.dumps([
            {"name": r["name"], "schedule": r.get("schedule"), "alert": r.get("alert"),
             "spec": r["spec"]} for r in watches
        ]))
        return
    if not watches:
        console.print("[yellow]No watches yet.[/yellow]")
        return
    for r in watches:
        console.print(f"  {r['name']}  max €{r['alert'].get('max_price')}  schedule={r.get('schedule')}")


@watch_app.command("rm")
def watch_rm(name: str = typer.Argument(...)):
    """Remove a watch (same store as searches rm)."""
    searches_rm(name)


# --------------------------------------------------------------------------- #
# brief — the monitoring loop (Task 8)                                         #
# --------------------------------------------------------------------------- #
@app.command()
def brief(
    send: bool = typer.Option(False, "--send", help="Send the digest to Telegram"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the Telegram chunks instead of sending"),
    all_searches: bool = typer.Option(False, "--all", help="Run every saved search, ignoring schedule"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass cache, fetch fresh prices"),
    max_calls: int = typer.Option(40, "--max-calls"),
    pretty: bool = typer.Option(False, "--pretty"),
):
    """Run every due saved search, diff confirmed prices against the alert
    state machine, fire exactly-once alerts, prune stale state, and emit one
    digest envelope. ``--send`` pushes it to Telegram; a failed send exits 1.
    A second concurrent brief exits 1 ("already running")."""
    from flight_deals import output
    from flight_deals.engine.brief import run_brief, should_send
    from flight_deals.state.store import flock_guard

    with flock_guard("brief"):
        result = run_brief(
            force_all=all_searches, registry=registry, history_store=history_store,
            max_calls=max_calls, fresh=fresh,
        )
        typer.echo(output.render(result.envelope, pretty=pretty))

        send_failed = False
        if dry_run:
            # Always preview the digest we *would* send, offline.
            digest = output.telegram_text(result.envelope, html=True)
            get_notifier().send(digest, dry_run=True)
        elif send and should_send(result):
            # Only message when there's something to report (no empty-digest spam).
            digest = output.telegram_text(result.envelope, html=True)
            if not get_notifier().send(digest):
                send_failed = True

        if send_failed:
            raise typer.Exit(1)
        if result.exit_code:
            raise typer.Exit(result.exit_code)


if __name__ == "__main__":
    app()
