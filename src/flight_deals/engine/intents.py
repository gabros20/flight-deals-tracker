"""Intent verbs: the thin builders behind ``getaway`` / ``oneway`` / ``check``
(UPGRADE-PLAN §4, SEARCH-DESIGN §5). Flags in, ``SearchSpec`` out, planner →
estimate→confirm → history enrichment → snapshot → frozen envelope.

Everything network- or clock-sensitive is injectable (``today``, ``planner``,
``snapshotter``) so the CLI stays a one-liner and the behaviour is testable
under freezegun and fixture-mocked providers.
"""

from __future__ import annotations

import difflib
import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flight_deals import output
from flight_deals.engine import combine, confirm as confirm_mod
from flight_deals.engine.planner import Planner, check_max_calls
from flight_deals.engine.spec import parse_spec
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.state import snapshots

logger = logging.getLogger(__name__)


class IntentError(ValueError):
    """A getaway/oneway/check input error. ``hint`` is an exact corrected
    command (CONTRACT §3, exit 2) so the CLI maps it straight to the envelope."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# Validation (before any network — UPGRADE-PLAN §4 "Validation before network")#
# --------------------------------------------------------------------------- #
def _validate_origins(raw_origins: List[str], registry: DestinationRegistry) -> List[str]:
    """Uppercase + fuzzy-match each origin against the registry. An unknown code
    yields a ``hint`` with the nearest known IATA ("BUDA -> did you mean BUD?")."""
    known = sorted({a.iata for a in registry.airports})
    out: List[str] = []
    for raw in raw_origins:
        code = str(raw).strip().upper()
        if code in known:
            out.append(code)
            continue
        close = difflib.get_close_matches(code, known, n=1, cutoff=0.5)
        if close:
            raise IntentError(
                f"unknown origin airport {code!r}",
                f"did you mean {close[0]}? e.g. --from {close[0]}",
            )
        raise IntentError(
            f"unknown origin airport {code!r}",
            f"{code!r} is not a known airport — run 'flight-deals where list' or use a 3-letter IATA like BUD",
        )
    return out


def _validate_not_past(depart_spec, today: date) -> None:
    """Reject a departure window that lies entirely in the past."""
    out_to = date.fromisoformat(depart_spec.out_to)
    if out_to < today:
        raise IntentError(
            f"departure window ends {out_to.isoformat()}, before today {today.isoformat()}",
            "pick a future date, e.g. --depart "
            f"{today.isoformat()}..{date.fromordinal(today.toordinal() + 3).isoformat()}",
        )


# --------------------------------------------------------------------------- #
# getaway / oneway                                                            #
# --------------------------------------------------------------------------- #
def run_search(
    *,
    where: Optional[str],
    depart: str,
    nights: Optional[str],
    budget: Optional[float],
    origins: List[str],
    to: Optional[str] = None,
    max_results: int = 10,
    max_calls: int = 40,
    fresh: bool = False,
    carriers: Optional[List[str]] = None,
    shapes: Optional[List[str]] = None,
    registry: Optional[DestinationRegistry] = None,
    planner: Optional[Planner] = None,
    history_store: Any = None,
    snapshotter: Callable[..., Any] = snapshots.snapshot,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    do_confirm: bool = True,
) -> Tuple[Dict[str, Any], int]:
    """Build a spec from intent flags, run it, confirm approximate deals, enrich
    from history, snapshot each displayed deal, and return ``(envelope, exit)``.
    ``nights=None`` -> one-way (S1); otherwise round-trip (S2). ``to`` is a
    named destination (IATA or city) resolved against the registry onto
    ``SearchSpec.destinations``; it is mutually exclusive with ``where``."""
    from flight_deals.engine.spec import SpecError

    registry = registry or DestinationRegistry()
    today = today or date.today()
    now = now or datetime.now(timezone.utc)

    if to is not None and where is not None:
        raise IntentError(
            "--to and --where are mutually exclusive",
            "a city is not a tag: name a place with --to (e.g. --to Barcelona) "
            "OR a category with --where (e.g. --where seaside), not both",
        )

    valid_origins = _validate_origins(origins, registry)

    spec_dict: Dict[str, Any] = {"origins": valid_origins, "depart": depart, "max_results": max_results}
    if where:
        spec_dict["where"] = where
    if to is not None:
        dests = registry.resolve_destination(to)
        if not dests:
            suggestion = registry.destination_suggestion(to)
            hint = (
                f"did you mean --to {suggestion}?" if suggestion else
                f"{to!r} is not a known airport or city — use a 3-letter IATA "
                "(e.g. --to BCN) or a city name (e.g. --to Barcelona)"
            )
            raise IntentError(f"unknown destination {to!r}", hint)
        spec_dict["destinations"] = dests
    if nights is not None:
        spec_dict["nights"] = nights
    if budget is not None:
        spec_dict["budget"] = budget
    if carriers:
        spec_dict["carriers"] = carriers
    if shapes:
        spec_dict["shapes"] = shapes

    spec = parse_spec(spec_dict)  # raises SpecError (exit 2 in CLI)
    _validate_not_past(spec.depart_spec, today)

    planner = planner or Planner(registry=registry)
    return execute_spec(
        spec,
        planner=planner,
        registry=registry,
        history_store=history_store,
        snapshotter=snapshotter,
        now=now,
        do_confirm=do_confirm,
        fresh=fresh,
        max_calls=max_calls,
    )


def execute_spec(
    spec,
    *,
    planner: Planner,
    registry: Optional[DestinationRegistry] = None,
    history_store: Any = None,
    snapshotter: Callable[..., Any] = snapshots.snapshot,
    now: Optional[datetime] = None,
    do_confirm: bool = True,
    fresh: bool = False,
    max_calls: int = 40,
) -> Tuple[Dict[str, Any], int]:
    """Run an already-parsed ``SearchSpec`` through the full intent pipeline
    (planner → estimate→confirm → history enrich → snapshot → envelope) and
    return ``(envelope, exit_code)``.

    Split out of :func:`run_search` so ``brief`` (Task 8) can drive a saved
    search's stored spec through the identical path — one pipeline, no second
    code path — without going through the intent-flag validation that only the
    interactive verbs need."""
    now = now or datetime.now(timezone.utc)

    plan = planner.compile(spec)
    check_max_calls(plan, max_calls)  # PlannerRefusal -> exit 2 in CLI
    outcome = planner.execute(plan, spec, fresh=fresh)

    results = outcome["results"]

    # Estimate→confirm: budget/top-N truncation above happened on *estimates*,
    # so confirm alone could only ever drop a deal (never rescue one just over
    # budget, nor back-fill a slot from one just outside the top-N). Instead we
    # confirm the wider (bounded) ``confirm_band`` planner.execute() computed —
    # estimates within BUDGET_MARGIN_FACTOR (20%) over budget and ranked up to
    # ``_confirm_band_size(max_results)`` — then re-apply the *strict* budget
    # filter, re-rank, and truncate to max_results using confirmed prices. The
    # extra confirm calls are bounded by the band size, never by the full
    # candidate pool (see planner._confirm_band_size).
    if do_confirm:
        band = outcome.get("confirm_band", results)
        if band:
            confirm_mod.confirm(band, wizz=planner.wizz, ryanair=planner.ryanair)
            if spec.budget is not None:
                band = [d for d in band if d["price_eur"] <= float(spec.budget)]
            band.sort(key=lambda d: (d["price_eur"], d["price_confidence"] != "exact", d["destination"]))
            results = band[: spec.max_results]
        else:
            results = []

    # History enrichment: honest why-strings + standout/solid/baseline groups.
    if history_store is None:
        from flight_deals.history import PriceHistoryStore
        history_store = PriceHistoryStore()
    combine.enrich(results, history_store)

    # Snapshot every displayed deal (durable, append-only observations).
    for d in results:
        try:
            snapshotter(d, now=now)
        except Exception as e:  # noqa: BLE001
            logger.warning("snapshot failed for %s: %s", d.get("deal_id"), e)

    # Recompute the empty-state: confirm may have dropped everything from a
    # previously non-empty set, or (the margin-band rescue/back-fill) turned a
    # previously empty set non-empty — route_status must track the *final*
    # results, not the pre-confirm ones.
    route_status = outcome["route_status"]
    exit_code = outcome["exit_code"]
    if results:
        route_status = None
        exit_code = 0  # CONTRACT §3: exit 1 only when results == []
    elif route_status is None:
        route_status = "no_match" if outcome["candidate_count"] else "no_service"

    error = hint = None
    if exit_code == 1:
        error = "provider_error"
        hint = "the next scheduled run will retry; or re-run with --fresh"

    env = output.envelope(
        results=results,
        summary=output.build_summary(results, spec.origins, route_status, outcome["sources"]),
        sources=outcome["sources"],
        next=output.build_next(spec, results, route_status),
        route_status=route_status,
        error=error,
        hint=hint,
    )
    return env, exit_code


# --------------------------------------------------------------------------- #
# check <deal_id>                                                             #
# --------------------------------------------------------------------------- #
def _requery(snap: Dict[str, Any], planner: Planner) -> Optional[Tuple[float, str, List[Dict[str, Any]]]]:
    """Live exact re-query for a snapshotted deal. Returns
    ``(price_eur, confidence, legs)`` or ``None`` if nothing bookable is found.
    Ryanair by exact roundTripFares/oneWayFares; Wizz by exact-date timetable."""
    origin, dest = snap["origin"], snap["destination"]
    out_date = snap["out_date"]
    return_date = snap.get("return_date")
    carriers = snap.get("carriers", [])

    if "ryanair" in carriers:
        if return_date:
            pairs = planner.ryanair.roundtrip_fares(
                origin, dest, out_from=out_date, out_to=out_date,
                ret_from=return_date, ret_to=return_date, use_cache=False,
            )
            match = next((p for p in pairs if p.out_date == out_date and p.return_date == return_date), None)
            if match is None and pairs:
                match = min(pairs, key=lambda p: p.total_price_eur)
            if match:
                legs = [
                    output.flight_leg(match.outbound.origin, match.outbound.destination, "ryanair",
                                      match.outbound.date, match.outbound.price_eur,
                                      departure_time=match.outbound.departure_time,
                                      flight_number=match.outbound.flight_number,
                                      duration_minutes=match.outbound.duration_minutes),
                    output.flight_leg(match.inbound.origin, match.inbound.destination, "ryanair",
                                      match.inbound.date, match.inbound.price_eur,
                                      departure_time=match.inbound.departure_time,
                                      flight_number=match.inbound.flight_number,
                                      duration_minutes=match.inbound.duration_minutes),
                ]
                return round(match.total_price_eur, 2), "exact", legs
        else:
            fares = planner.ryanair.oneway_fares(origin, dest, out_from=out_date, out_to=out_date, use_cache=False)
            hit = next((f for f in fares if f.date == out_date), None)
            if hit:
                legs = [output.flight_leg(hit.origin, hit.destination, "ryanair", hit.date, hit.price_eur,
                                          departure_time=hit.departure_time, flight_number=hit.flight_number)]
                return round(hit.price_eur, 2), "exact", legs

    if "wizzair" in carriers:
        lo, hi = out_date, (return_date or out_date)
        out_fares, ret_fares = planner.wizz.timetable(origin, dest, lo, hi, use_cache=False)
        out_hit = next((f for f in out_fares if f.date == out_date), None)
        if out_hit is None:
            return None
        if return_date:
            ret_hit = next((f for f in ret_fares if f.date == return_date), None)
            if ret_hit is None:
                return None
            legs = [
                output.flight_leg(out_hit.origin, out_hit.destination, "wizzair", out_hit.date, out_hit.price_eur,
                                  departure_time=out_hit.departure_time),
                output.flight_leg(ret_hit.origin, ret_hit.destination, "wizzair", ret_hit.date, ret_hit.price_eur,
                                  departure_time=ret_hit.departure_time),
            ]
            return round(out_hit.price_eur + ret_hit.price_eur, 2), "approximate", legs
        legs = [output.flight_leg(out_hit.origin, out_hit.destination, "wizzair", out_hit.date, out_hit.price_eur,
                                  departure_time=out_hit.departure_time)]
        return round(out_hit.price_eur, 2), "approximate", legs

    return None


def check_deal(
    deal_id: str,
    *,
    planner: Optional[Planner] = None,
    registry: Optional[DestinationRegistry] = None,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    snapshotter: Callable[..., Any] = snapshots.snapshot,
) -> Tuple[Dict[str, Any], int]:
    """Re-check a snapshotted deal: live exact re-query → delta vs latest and
    first observation. Unknown id or past-dated deal → exit 2 with a hint."""
    today = today or date.today()
    now = now or datetime.now(timezone.utc)

    snap = snapshots.latest(deal_id)
    if snap is None:
        env = output.error_envelope(
            "unknown_deal",
            f"no snapshots for deal '{deal_id}' — find a deal first with "
            "'flight-deals getaway --depart <window> --where <expr> --nights <range>'",
        )
        return env, 2

    out_date = date.fromisoformat(snap["out_date"])
    if out_date < today:
        env = output.error_envelope(
            "dates_passed",
            f"deal {deal_id} departs {snap['out_date']}, which has passed — "
            "run 'flight-deals getaway --depart <future window> --where <expr> --nights <range>'",
        )
        return env, 2

    registry = registry or DestinationRegistry()
    planner = planner or Planner(registry=registry)
    first_snap = snapshots.first(deal_id)

    try:
        requeried = _requery(snap, planner)
        provider_status = {c: "ok" for c in snap.get("carriers", [])}
    except Exception as e:  # noqa: BLE001
        logger.warning("check: re-query failed for %s: %s", deal_id, e)
        from flight_deals.orchestrator import status_for_exception
        carriers = snap.get("carriers", ["ryanair"])
        provider_status = {("wizzair" if c == "wizzair" else "ryanair"): status_for_exception(e) for c in carriers}
        env = output.envelope(
            results=[], summary="could not re-check this deal — a provider failed",
            sources=provider_status, next=[], route_status="provider_error",
            error="provider_error", hint="the next scheduled run will retry; or re-run with --fresh",
        )
        return env, 1

    if requeried is None:
        env = output.envelope(
            results=[], summary=f"{snap['origin']}→{snap['destination']} {snap['out_date']} "
                                "no longer has a bookable fare on these dates",
            sources=provider_status, next=[], route_status="no_service",
        )
        return env, 0

    price, confidence, legs = requeried
    deal = output.build_deal(
        shape=snap["shape"], origin=snap["origin"], destination=snap["destination"],
        out_date=snap["out_date"], return_date=snap.get("return_date"),
        price_eur=price, price_confidence=confidence, carriers=snap["carriers"], legs=legs,
        why=output.why_string(price, confidence, round_trip=snap.get("return_date") is not None),
    )
    snapshotter(deal, now=now)

    last_price = snap["price_eur"]
    first_price = first_snap["price_eur"] if first_snap else last_price
    delta_last = round(price - last_price, 2)
    delta_first = round(price - first_price, 2)
    arrow = "unchanged" if delta_last == 0 else (f"up €{delta_last:.0f}" if delta_last > 0 else f"down €{-delta_last:.0f}")
    summary = (
        f"{snap['origin']}→{snap['destination']} {snap['out_date']} now €{price:.0f} "
        f"({arrow} vs last €{last_price:.0f}; €{delta_first:+.0f} vs first €{first_price:.0f})"
    )
    env = output.envelope(results=[deal], summary=summary, sources=provider_status, next=[])
    env["delta"] = {
        "current_price_eur": price,
        "last_price_eur": last_price,
        "first_price_eur": first_price,
        "delta_vs_last_eur": delta_last,
        "delta_vs_first_eur": delta_first,
        "first_seen_at": first_snap["seen_at"] if first_snap else snap["seen_at"],
    }
    return env, 0
