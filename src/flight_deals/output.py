"""THE renderer (Global Constraint 2 / CONTRACT §1-6).

One module owns every byte that reaches a user or an agent:

* ``deal_id`` — the frozen id derivation (CONTRACT §5);
* ``build_deal`` — a Deal object (CONTRACT §2) from resolved fare data;
* ``envelope`` — the top-level object (CONTRACT §1);
* ``render`` — JSON (canonical) or ``--pretty`` human text from the SAME fields
  (no second data path);
* ``telegram_text`` — the digest string Task 8 sends, built from the same
  envelope;
* ``build_summary`` / ``build_next`` — the honest one-sentence summary and the
  at-most-one widening move (CONTRACT / UPGRADE-PLAN §4).

Nothing here touches the network or invents data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# deal_id (CONTRACT §5, frozen — pinned by the golden vector test)            #
# --------------------------------------------------------------------------- #
def deal_id(
    origin: str,
    destination: str,
    out_date: str,
    return_date: Optional[str],
    shape: str,
    carriers: List[str],
) -> str:
    key = "|".join([
        origin.upper(),
        destination.upper(),
        out_date,
        return_date or "",  # "" for one-way, never the string "None"
        shape,
        "+".join(sorted(c.lower() for c in carriers)),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# Booking deep links                                                          #
# --------------------------------------------------------------------------- #
def _ryanair_link(origin: str, dest: str, out_date: str, return_date: Optional[str]) -> str:
    base = (
        "https://www.ryanair.com/gb/en/trip/flights/select"
        f"?originIata={origin}&destinationIata={dest}&dateOut={out_date}&adults=1"
    )
    if return_date:
        base += f"&dateIn={return_date}"
    return base


def _wizzair_link(origin: str, dest: str, out_date: str, return_date: Optional[str]) -> str:
    if return_date:
        return f"https://wizzair.com/en-gb/booking/select-flight/{origin}/{dest}/{out_date}/{return_date}"
    return f"https://wizzair.com/en-gb/booking/select-flight/{origin}/{dest}/{out_date}"


def _links(carriers: List[str], origin: str, dest: str, out_date: str, return_date: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in carriers:
        if c == "ryanair":
            out["ryanair"] = _ryanair_link(origin, dest, out_date, return_date)
        elif c == "wizzair":
            out["wizzair"] = _wizzair_link(origin, dest, out_date, return_date)
    return out


# --------------------------------------------------------------------------- #
# Deal object (CONTRACT §2)                                                    #
# --------------------------------------------------------------------------- #
def flight_leg(
    origin: str,
    destination: str,
    carrier: str,
    departure_date: str,
    price_eur: float,
    *,
    departure_time: Optional[str] = None,
    flight_number: Optional[str] = None,
    duration_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "type": "flight",
        "origin": origin,
        "destination": destination,
        "carrier": carrier,
        "departure_date": departure_date,
        "departure_time": departure_time,
        "flight_number": flight_number,
        "price_eur": round(float(price_eur), 2),
        "duration_minutes": duration_minutes,
    }


def build_deal(
    *,
    shape: str,
    origin: str,
    destination: str,
    out_date: str,
    return_date: Optional[str],
    price_eur: float,
    price_confidence: str,
    carriers: List[str],
    legs: List[Dict[str, Any]],
    why: str,
    ground: Optional[Dict[str, Any]] = None,
    estimated_price_eur: Optional[float] = None,
    group: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble one Deal dict per CONTRACT §2. ``carriers`` is sorted (feeds the
    frozen ``deal_id``); ``nights`` is computed from the dates so it can never
    disagree with them.

    ``estimated_price_eur`` (Task 7, estimate→confirm): when an approximate
    (Wizz) fare has been confirmed via an exact-date re-query, the confirmed
    figure lands in ``price_eur`` and the original windowed estimate is retained
    here. ``group`` (Task 7, history enrichment): ``standout|solid|baseline``.
    Both are only attached when set, so the deterministic planner path (which
    does neither) keeps producing byte-identical envelopes."""
    carriers = sorted(c.lower() for c in carriers)
    from datetime import date as _date

    nights: Optional[int] = None
    if return_date:
        nights = (_date.fromisoformat(return_date) - _date.fromisoformat(out_date)).days
    deal: Dict[str, Any] = {
        "deal_id": deal_id(origin, destination, out_date, return_date, shape, carriers),
        "shape": shape,
        "origin": origin,
        "destination": destination,
        "out_date": out_date,
        "return_date": return_date,
        "nights": nights,
        "price_eur": round(float(price_eur), 2),
        "price_confidence": price_confidence,
        "carriers": carriers,
        "legs": legs,
        "ground": ground,
        "why": why,
        "links": _links(carriers, origin, destination, out_date, return_date),
    }
    if estimated_price_eur is not None:
        deal["estimated_price_eur"] = round(float(estimated_price_eur), 2)
    if group is not None:
        deal["group"] = group
    return deal


def standout_group(deal: Dict[str, Any], history: Optional[Dict[str, Any]] = None) -> str:
    """Grouping hook (SEARCH-DESIGN §2): ``standout`` (>=25% below typical) /
    ``solid`` / ``baseline``. Task 7 wires real history; until then, with no
    comparison basis, everything is honestly ``baseline`` — this is the seam
    Task 7 fills, not a fabricated percentile."""
    if not history or history.get("count", 0) < 5:
        return "baseline"
    typical = history.get("median_price")
    if typical and deal["price_eur"] <= 0.75 * typical:
        return "standout"
    if typical and deal["price_eur"] < typical:
        return "solid"
    return "baseline"


def why_string(price_eur: float, price_confidence: str, round_trip: bool) -> str:
    """A minimal, factual, non-comparative ``why`` (CONTRACT §2 — history
    enrichment upgrades this in Task 7)."""
    trip = "round-trip" if round_trip else "one-way"
    if price_confidence == "exact":
        return f"€{price_eur:.0f} {trip}, exact Ryanair fare"
    return f"~€{price_eur:.0f} {trip} estimate, Wizz timetable (approximate)"


# --------------------------------------------------------------------------- #
# sources projection (single status representation -> frozen sources map)      #
# --------------------------------------------------------------------------- #
def project_sources(aggregated: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Project ``orchestrator.aggregate_status`` output ({provider:{ok,status,
    ...}}) down to the frozen ``sources`` map ({provider: status_string},
    CONTRACT §1). Deterministic key order."""
    return {name: entry.get("status", "ok" if entry.get("ok") else "error")
            for name, entry in sorted(aggregated.items())}


# --------------------------------------------------------------------------- #
# summary + next                                                              #
# --------------------------------------------------------------------------- #
_ROUTE_STATUS_PROSE = {
    "no_service": "no scheduled service was found for this search in the window",
    "no_match": "service exists but nothing matched the budget/constraints",
    "provider_error": "a provider failed, so this run could not confirm what's available",
}

# Status values that mean "this provider's data is NOT usable this run"
# (CONTRACT §1 sources enum) — everything else (``ok``, ``version_refreshed``)
# is a clean success for coverage-gap purposes.
_SOURCE_FAILURE_STATUSES = {"error", "blocked", "parse_error"}

_PROVIDER_DISPLAY_NAME = {"ryanair": "Ryanair", "wizzair": "Wizz Air"}


def _coverage_gap_note(sources: Optional[Dict[str, str]]) -> str:
    """CONTRACT §3 "Partial coverage": when results are non-empty but a
    provider failed, ``summary`` must name the gap in plain language rather
    than reading as a clean success."""
    if not sources:
        return ""
    failing = [name for name, status in sorted(sources.items()) if status in _SOURCE_FAILURE_STATUSES]
    if not failing:
        return ""
    names = " and ".join(_PROVIDER_DISPLAY_NAME.get(n, n) for n in failing)
    return f" ({names} unavailable — results may be incomplete)"


def build_summary(
    results: List[Dict[str, Any]],
    origins: List[str],
    route_status: Optional[str],
    sources: Optional[Dict[str, str]] = None,
) -> str:
    """One honest sentence, safe to paste into Telegram (CONTRACT §1). When
    ``results`` is non-empty but ``sources`` shows a provider failure, appends
    the coverage-gap caveat (CONTRACT §3 "Partial coverage")."""
    origin_str = "/".join(origins)
    if not results:
        return _ROUTE_STATUS_PROSE.get(route_status or "no_service", "no deals found")
    cheapest = results[0]
    conf = "exact" if cheapest["price_confidence"] == "exact" else "approx"
    dates = cheapest["out_date"]
    trip = "ow"
    if cheapest.get("return_date"):
        dates += f"-{cheapest['return_date']}"
        trip = "rt"
    n = len(results)
    plural = "deal" if n == 1 else "deals"
    return (
        f"Found {n} {plural} from {origin_str}, cheapest {cheapest['destination']} "
        f"€{cheapest['price_eur']:.0f} {trip} {dates} ({conf})"
    ) + _coverage_gap_note(sources)


def _spec_run_command(spec: Any, **overrides: Any) -> str:
    """A copy-pasteable ``run --spec '{json}'`` string built from ``spec`` with
    the given field overrides applied (the one widening move)."""
    body: Dict[str, Any] = {"origins": list(spec.origins)}
    where = getattr(spec, "where", None)
    if where:
        body["where"] = where
    body["depart"] = getattr(spec, "depart", None)
    if getattr(spec, "nights", None) is not None:
        body["nights"] = spec.nights
    if getattr(spec, "budget", None) is not None:
        body["budget"] = spec.budget
    body.update(overrides)
    return f"flight-deals run --spec '{json.dumps(body, separators=(',', ':'))}'"


def _widen_depart(spec: Any, extra_days: int = 3) -> str:
    """Extend the outbound window's tail by ``extra_days`` (the cheapest window
    widening — a few more candidate dates, no new routes)."""
    from datetime import date as _date, timedelta as _td

    ds = spec.depart_spec
    new_to = (_date.fromisoformat(ds.out_to) + _td(days=extra_days)).isoformat()
    return f"{ds.out_from}..{new_to}"


def _broaden_where(where: Optional[str]) -> Optional[str]:
    """Drop the last ``& term`` of a where-expression so the category widens
    (e.g. ``"seaside & italy"`` -> ``"seaside"``). ``None`` if nothing to drop."""
    if not where or "&" not in where:
        return None
    return where.rsplit("&", 1)[0].strip()


def build_next(spec: Any, results: List[Dict[str, Any]], route_status: Optional[str]) -> List[str]:
    """At most ONE widening move (CONTRACT / UPGRADE-PLAN §4). Copy-pasteable
    command strings only. The single move is the *cheapest that plausibly
    helps*: budget +20% when the emptiness is a budget/constraint miss with a
    budget set; a window +3d extension when service exists seasonally; else a
    category broaden. Empty when nothing sensible remains."""
    if results:
        # A single, useful follow-up: check the cheapest deal's live price/history.
        return [f"flight-deals check {results[0]['deal_id']}"]

    # Empty result: offer exactly one widening move, chosen by why-it's-empty.
    if route_status == "no_match" and getattr(spec, "budget", None):
        new_budget = int(round(float(spec.budget) * 1.2))
        return [_spec_run_command(spec, budget=new_budget)]

    # no_service (or no_match without a budget to raise): a seasonal window is
    # the likeliest cheap unlock — widen the depart window by 3 days.
    try:
        return [_spec_run_command(spec, depart=_widen_depart(spec))]
    except Exception:
        broadened = _broaden_where(getattr(spec, "where", None))
        if broadened:
            return [_spec_run_command(spec, where=broadened)]
        return []


# --------------------------------------------------------------------------- #
# envelope + render                                                           #
# --------------------------------------------------------------------------- #
def envelope(
    results: List[Dict[str, Any]],
    summary: str,
    sources: Dict[str, str],
    next: List[str],
    *,
    route_status: Optional[str] = None,
    error: Optional[str] = None,
    hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the top-level envelope (CONTRACT §1). ``route_status`` is only
    attached when ``results`` is empty (frozen invariant); ``error``/``hint`` are
    attached together only when present."""
    env: Dict[str, Any] = {
        "results": results,
        "summary": summary,
        "sources": sources,
        "next": next,
    }
    if not results and route_status is not None:
        env["route_status"] = route_status
    if error is not None:
        env["error"] = error
        env["hint"] = hint or ""
    return env


def error_envelope(error: str, hint: str) -> Dict[str, Any]:
    """The exit-2 shape: structured failure, empty results, paired error/hint."""
    return {
        "results": [],
        "summary": hint or error,
        "sources": {},
        "next": [],
        "error": error,
        "hint": hint,
    }


def render(env: Dict[str, Any], pretty: bool = False) -> str:
    """Canonical JSON (default) or human text (``--pretty``) from the SAME
    fields. JSON keys are NOT sorted so ``results`` keeps rank order."""
    if not pretty:
        return json.dumps(env, ensure_ascii=False)
    return _pretty(env)


def _pretty(env: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(env.get("summary", ""))
    for i, d in enumerate(env.get("results", []), 1):
        dates = d["out_date"] + (f" -> {d['return_date']}" if d.get("return_date") else "")
        lines.append(
            f"  {i}. {d['origin']}->{d['destination']}  €{d['price_eur']:.2f} "
            f"{dates}  [{d['price_confidence']}, {'+'.join(d['carriers'])}]  {d['deal_id']}"
        )
        lines.append(f"       {d['why']}")
    src = env.get("sources", {})
    if src:
        lines.append("  sources: " + ", ".join(f"{k}={v}" for k, v in src.items()))
    if env.get("route_status"):
        lines.append(f"  route_status: {env['route_status']}")
    if env.get("error"):
        lines.append(f"  error: {env['error']}")
        lines.append(f"  hint: {env['hint']}")
    for nxt in env.get("next", []):
        lines.append(f"  next: {nxt}")
    return "\n".join(lines)


def telegram_text(env: Dict[str, Any]) -> str:
    """A digest string built from the SAME envelope (used by Task 8's notifier).
    Plain text, safe as a Telegram message body."""
    lines = [env.get("summary", "")]
    for i, d in enumerate(env.get("results", [])[:10], 1):
        dates = d["out_date"] + (f"–{d['return_date']}" if d.get("return_date") else "")
        conf = "" if d["price_confidence"] == "exact" else " ~"
        lines.append(
            f"{i}. {d['origin']}→{d['destination']} €{d['price_eur']:.0f}{conf} {dates}"
        )
    return "\n".join(lines)
