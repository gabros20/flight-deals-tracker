"""``brief`` — the deterministic monitoring loop (UPGRADE-PLAN §6, SEARCH-DESIGN
§6, brief req 4).

One cron entry point runs every *due* saved search, diffs the fresh confirmed
prices against the alert-state machine, fires exactly-once Telegram alerts,
prunes stale state, and emits ONE envelope whose ``summary`` is the digest.
There is no model in this loop — it is the reliability backbone; the agentic
loop (Task 9) lives beside it, never inside it.

Flow (per UPGRADE-PLAN §6): the caller holds an ``flock`` so only one brief runs
at a time; this function then:

1. picks the due searches (``searches.due``; ``--all`` forces every one);
2. runs each through ``intents.execute_spec`` — the *same* planner →
   estimate→confirm → history-enrich → snapshot pipeline the intent verbs use
   (so only confirmed-exact prices ever reach the alert machine);
3. appends each displayed deal to the price-history CSV (the "collect" role —
   keeps typical-price context fresh, UPGRADE-PLAN §6);
4. evaluates each watch's alert threshold against the confirmed prices;
5. prunes past-dated snapshots, expired cache entries, stale run stamps and
   expired alert entries;
6. builds one envelope: ``results`` = alerting deals + top movers, ``summary`` =
   the human digest sentence.

Everything network/clock/IO-touching is injectable so the whole loop is
freezegun- + fixture-testable with a fake notifier.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flight_deals import output
from flight_deals.config import get_config
from flight_deals.engine import intents
from flight_deals.engine.planner import Planner, PlannerRefusal
from flight_deals.engine.spec import SpecError, parse_spec
from flight_deals.models import PriceSnapshot
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.state import alert_state, searches, snapshots

logger = logging.getLogger(__name__)

MAX_MOVERS = 5


class BriefResult:
    """Outcome of one brief run: the envelope, an exit code, and the fired
    alert deals (so the CLI can decide whether to send and the tests can assert
    exactly-once)."""

    def __init__(self, envelope: Dict[str, Any], exit_code: int, fired: List[Dict[str, Any]], ran: List[str]):
        self.envelope = envelope
        self.exit_code = exit_code
        self.fired = fired
        self.ran = ran


def should_send(result: "BriefResult") -> bool:
    """Whether a ``--send`` brief should actually message Telegram: only when
    there is something to report (an alert fired or a mover surfaced). This is
    what makes an hourly cron quiet — a run with nothing new sends nothing,
    while the alert machine still guarantees a real drop is never missed."""
    return bool(result.envelope.get("results"))


def _to_history_snapshot(deal: Dict[str, Any], now: datetime) -> PriceSnapshot:
    return PriceSnapshot(
        timestamp_utc=now,
        origin=deal["origin"],
        destination=deal["destination"],
        departure_date=deal["out_date"],
        return_date=deal.get("return_date"),
        price=float(deal["price_eur"]),
        currency="EUR",
        source="+".join(deal.get("carriers", [])) or "brief",
        total_price=float(deal["price_eur"]),
    )


def _prior_price(deal_id: str) -> Optional[float]:
    """The price of the previous distinct observation for this deal (used to
    surface movers). ``None`` when there is no earlier observation."""
    recs = snapshots.records(deal_id)
    if len(recs) < 2:
        return None
    return float(recs[-2]["price_eur"])


def run_brief(
    *,
    force_all: bool = False,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
    planner: Optional[Planner] = None,
    registry: Optional[DestinationRegistry] = None,
    history_store: Any = None,
    alert_machine: Optional[alert_state.AlertMachine] = None,
    config: Any = None,
    max_calls: int = 40,
    fresh: bool = False,
    snapshotter: Callable[..., Any] = snapshots.snapshot,
    do_prune: bool = True,
) -> BriefResult:
    """Run the monitoring loop. Does NOT take the flock (the CLI does, so a bad
    flock exits before any work) and does NOT send Telegram (the CLI does, so a
    dry-run stays offline). Returns a :class:`BriefResult`."""
    now = now or datetime.now(timezone.utc)
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today = today or now.astimezone(timezone.utc).date()

    config = config or get_config()
    registry = registry or DestinationRegistry()
    planner = planner or Planner(registry=registry)
    if history_store is None:
        from flight_deals.history import PriceHistoryStore
        history_store = PriceHistoryStore()
    alert_machine = alert_machine or alert_state.AlertMachine(realert_drop_pct=config.realert_drop_pct)

    due = searches.due(now, force_all=force_all)

    sources: Dict[str, str] = {}
    fired: List[Dict[str, Any]] = []
    movers: List[Tuple[float, Dict[str, Any]]] = []  # (drop_amount, deal)
    ran: List[str] = []
    n_searches_failed = 0

    for record in due:
        name = record["name"]
        try:
            spec = parse_spec(record["spec"])
        except SpecError as e:
            logger.error("brief: saved search %r has an invalid spec, skipping: %s", name, e.message)
            n_searches_failed += 1
            continue

        try:
            env, code = intents.execute_spec(
                spec,
                planner=planner,
                registry=registry,
                history_store=history_store,
                snapshotter=snapshotter,
                now=now,
                fresh=fresh,
                max_calls=max_calls,
            )
        except PlannerRefusal as e:
            logger.error("brief: saved search %r refused by planner, skipping: %s", name, e.message)
            n_searches_failed += 1
            continue

        ran.append(name)
        searches.stamp_run(name, now)

        # Merge per-provider status (a failure anywhere sticks — worst wins).
        for prov, status in env.get("sources", {}).items():
            if prov not in sources or status != "ok":
                sources[prov] = status

        deals = env.get("results", [])
        alert_cfg = record.get("alert") or {}
        max_price = alert_cfg.get("max_price")

        for deal in deals:
            # Collect: keep the price-context CSV fresh.
            try:
                history_store.append(_to_history_snapshot(deal, now))
            except Exception as e:  # noqa: BLE001
                logger.warning("brief: history append failed for %s: %s", deal.get("deal_id"), e)

            # Alert: only watches (searches with an alert block) evaluate.
            fired_here = False
            if max_price is not None:
                if alert_machine.evaluate(search_name=name, deal=deal, max_price=float(max_price), now=now):
                    d = dict(deal)
                    d["_watch"] = name
                    fired.append(d)
                    fired_here = True

            # Mover: a confirmed drop vs the previous observation (for the digest
            # top-movers section), excluding deals already alerting.
            if not fired_here:
                prior = _prior_price(deal["deal_id"])
                if prior is not None and deal["price_eur"] < prior:
                    movers.append((round(prior - deal["price_eur"], 2), deal))

    # Persist alert state (one atomic write for the whole run).
    alert_machine.save()

    # Prune (bounded, git-friendly data dir) — UPGRADE-PLAN §4.
    if do_prune:
        try:
            snapshots.prune_past_dated(today)
            from flight_deals.cache import ResponseCache
            ResponseCache().prune_expired()
            searches.prune_stale_runs()
            alert_machine.prune_expired(now)
            alert_machine.save()
        except Exception as e:  # noqa: BLE001
            logger.warning("brief: prune pass had a problem (non-fatal): %s", e)

    # One envelope: alerting deals first, then top movers.
    movers.sort(key=lambda m: (-m[0], m[1]["price_eur"], m[1]["destination"]))
    mover_deals = [m[1] for m in movers[:MAX_MOVERS]]
    results = fired + mover_deals

    summary = _digest_summary(len(due), ran, fired, mover_deals, sources)
    envelope = output.envelope(results=results, summary=summary, sources=sources, next=[])
    envelope["brief"] = {
        "searches_due": len(due),
        "searches_ran": ran,
        "alerts": len(fired),
        "movers": len(mover_deals),
    }

    # Exit code: a search failing to even run is a real problem (exit 1); a
    # provider hiccup that still produced deals is not (the digest names it).
    exit_code = 1 if (n_searches_failed and not ran) else 0
    return BriefResult(envelope, exit_code, fired, ran)


def _digest_summary(
    n_due: int,
    ran: List[str],
    fired: List[Dict[str, Any]],
    movers: List[Dict[str, Any]],
    sources: Dict[str, str],
) -> str:
    """A single, Telegram-safe digest sentence (CONTRACT §1: pasteable, no
    markup)."""
    if n_due == 0:
        return "Brief: no saved searches were due."
    parts = [f"Brief ran {len(ran)} search{'es' if len(ran) != 1 else ''}"]
    if fired:
        cheapest = min(fired, key=lambda d: d["price_eur"])
        parts.append(
            f"{len(fired)} price alert{'s' if len(fired) != 1 else ''} "
            f"(cheapest €{cheapest['price_eur']:.0f} {cheapest['origin']}→{cheapest['destination']})"
        )
    else:
        parts.append("no alerts")
    if movers:
        parts.append(f"{len(movers)} mover{'s' if len(movers) != 1 else ''}")
    failing = [p for p, s in sorted(sources.items()) if s in {"error", "blocked", "parse_error"}]
    tail = ""
    if failing:
        tail = f" ({', '.join(failing)} unavailable — results may be incomplete)"
    return ", ".join(parts) + "." + tail
