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
    gem_slug: Optional[str] = None,
) -> str:
    key = "|".join([
        origin.upper(),
        destination.upper(),
        out_date,
        return_date or "",  # "" for one-way, never the string "None"
        shape,
        "+".join(sorted(c.lower() for c in carriers)),
    ])
    # Gem onward-extension (Task 15, CONTRACT §5 changelog 2026-07-12): an
    # additive, APPEND-ONLY "|gem:<slug>" component so a gem-extended deal (the
    # gateway flight PLUS the onward ferry/bus chain) gets a distinct id from
    # the plain gateway deal. Absent when no gem is attached, so every existing
    # id is byte-identical.
    if gem_slug:
        key += f"|gem:{gem_slug}"
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


def ground_leg(
    from_iata: str,
    to_iata: str,
    mode: str,
    duration_minutes: int,
    *,
    cost_eur: Optional[float] = None,
    distance_km: Optional[float] = None,
) -> Dict[str, Any]:
    """A ground-transfer leg dict (CONTRACT §2). ``cost_eur`` is the estimate for
    THIS leg (an S3 round-trip ground is two such legs; an S4 hop is one)."""
    return {
        "type": "ground",
        "from_iata": from_iata,
        "to_iata": to_iata,
        "mode": mode,
        "duration_minutes": duration_minutes,
        "distance_km": distance_km,
        "cost_eur": None if cost_eur is None else round(float(cost_eur), 2),
    }


def ground_summary(duration_minutes: int, cost_eur: Optional[float], mode: str,
                   estimate_basis: Optional[str] = None,
                   has_ferry: Optional[bool] = None,
                   transit_transfers: Optional[int] = None) -> Dict[str, Any]:
    """The Deal-level ``ground`` convenience mirror (CONTRACT §2): total ground
    duration + total cost across all ground legs of the trip, plus the mode.

    ``estimate_basis`` (Task 11, additive): ``"curated"`` for a hand-verified
    ground hop (the 6 curated open-jaw pairs, the VIE/BTS extended-origin legs),
    ``"computed"`` for one derived from the OSRM ground matrix, ``"scheduled"``
    (Task 13) for a pure Transitous-refined hop, or ``"scheduled-hybrid"`` (Task
    14) for a hop whose city line-haul is scheduled but whose airport-access pads
    are modeled. Only attached when set, so deals with no ground-provenance info
    stay byte-identical.

    ``has_ferry`` (Task 12, additive): ``True`` when the ground hop crosses water
    on a ferry (a curated ferry corridor or a computed ``ferry+ground`` matrix
    pair). Only attached when ``True`` so non-ferry deals stay byte-identical —
    agents disclose the crossing (⛴ in the why-string) before the user commits.

    ``transit_transfers`` (Task 13, additive): the number of transfers in the
    real Transitous scheduled itinerary backing a ``"scheduled"`` (or Task 14
    ``"scheduled-hybrid"``) hop. Only attached when set (scheduled/hybrid pairs),
    so other deals stay byte-identical."""
    out: Dict[str, Any] = {
        "duration_minutes": duration_minutes,
        "cost_eur": None if cost_eur is None else round(float(cost_eur), 2),
        "mode": mode,
    }
    if estimate_basis is not None:
        out["estimate_basis"] = estimate_basis
    if has_ferry:
        out["has_ferry"] = True
    if transit_transfers is not None:
        out["transit_transfers"] = transit_transfers
    return out


def connection_summary(hub: str, connect_out_minutes: int, connect_ret_minutes: int,
                       *, buffer_eur: Optional[float] = None) -> Dict[str, Any]:
    """The Deal-level ``connection`` object for an S5 via-hub self-transfer
    (Task 16, additive — CONTRACT §2b). Carries the hub, both VERIFIED
    same-airport connection gaps (minutes), the displayed self-transfer risk
    buffer, and the two hard honesty flags: ``verified: true`` (a displayed S5 is
    always time-verified) and ``separate_tickets: true`` (a missed connection is
    the traveller's own risk). Only ever attached to an S5 deal, so every other
    shape stays byte-identical."""
    out: Dict[str, Any] = {
        "hub": hub,
        "connect_out_minutes": connect_out_minutes,
        "connect_ret_minutes": connect_ret_minutes,
        "verified": True,
        "separate_tickets": True,
    }
    if buffer_eur is not None:
        out["buffer_eur"] = round(float(buffer_eur), 2)
    return out


def _fmt_hm(minutes: Optional[int]) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def ground_why_suffix(deal: Dict[str, Any]) -> str:
    """The honest ground-transfer clause appended to a shaped deal's ``why``
    (SEARCH-DESIGN §2). Empty for S1/S2 (no ground). For S3 it names the
    round-trip bus/train to the extended origin ("incl. ~€42 bus BUD⇄VIE,
    2×2h45m"); for S4 the open-jaw hop ("fly into NAP, train ~4h ~€35, fly home
    from BRI"). Both cost figures carry the ``~`` estimate marker (ground costs
    are static-curated estimates). Derived from the deal's own ``ground``/
    ``legs`` so it can never drift from the priced legs."""
    shape = deal.get("shape")
    g = deal.get("ground")
    if not g:
        return ""
    mode = g.get("mode", "transfer")
    cost = g.get("cost_eur")
    cost_str = f"€{cost:.0f}" if cost is not None else ""
    flight_legs = [l for l in deal.get("legs", []) if l.get("type") == "flight"]
    if shape == "S3":
        base = deal["origin"]
        via = flight_legs[0]["origin"] if flight_legs else "?"
        one_way = _fmt_hm((g.get("duration_minutes") or 0) // 2)
        pieces = [p for p in (f"~{cost_str}" if cost_str else "", mode, f"{base}⇄{via}") if p]
        tail = f" (2×{one_way})" if one_way else ""
        return f" incl. {' '.join(pieces)}{tail}"
    if shape == "S4":
        d1 = deal["destination"]
        d2 = flight_legs[-1]["origin"] if flight_legs else "?"
        dur = _fmt_hm(g.get("duration_minutes"))
        # A "scheduled" hop (Task 13) is backed end-to-end by a real Transitous
        # timetable: DROP the '~' on DURATION (a booked itinerary length, not a
        # stated estimate) and add the word "scheduled". A "scheduled-hybrid" hop
        # (Task 14) has a real scheduled CITY line-haul but MODELED airport-access
        # pads, so it KEEPS '~' on duration (the pads are estimates) and says
        # "line-haul scheduled" for honest disclosure. Both KEEP '~' on COST
        # (fares stay modeled — Transitous has no fares). Modeled/curated hops keep
        # '~' on both. The ~ markers are thus split per field AND per basis.
        basis = g.get("estimate_basis")
        drop_tilde = basis == "scheduled"  # only a pure-scheduled hop drops the ~
        dur_piece = dur if (drop_tilde and dur) else (f"~{dur}" if dur else "")
        cost_piece = f"~{cost_str}" if cost_str else ""
        sched_word = ("scheduled" if basis == "scheduled"
                      else "line-haul scheduled" if basis == "scheduled-hybrid"
                      else "")
        if g.get("has_ferry"):
            # Ferry hop: lead with ⛴ so an agent discloses the sea crossing
            # before the user gets attached to a price (Task 12).
            hop = " ".join(p for p in ("⛴", dur_piece, sched_word, cost_piece, "ferry") if p)
        else:
            hop = " ".join(p for p in (mode, dur_piece, sched_word, cost_piece) if p)
        return f" (fly into {d1}, {hop}, fly home from {d2})"
    return ""


def onward_why_suffix(deal: Dict[str, Any]) -> str:
    """The honest onward-chain clause appended to a gem-extended deal's ``why``
    (Task 15 / SEARCH-DESIGN §2b). Empty when the deal has no ``onward``. Names
    each hop's mode + duration (ferry legs lead with ⛴ so an agent discloses the
    sea crossing), then the gem name and the shape-adjusted round-trip/one-way
    onward cost with the ``~`` estimate marker (all onward costs are curated
    estimates). Derived from the deal's own ``onward`` so it can't drift.

    A marginal gem (reached via ``--to``) also surfaces the head of its gateway
    note prominently — those are the day-trip/awkward-connection caveats."""
    o = deal.get("onward")
    if not o:
        return ""
    parts: List[str] = []
    for leg in o.get("legs", []):
        hm = _fmt_hm(leg.get("duration_minutes"))
        mode = leg.get("mode", "transfer")
        parts.append(f"⛴ {hm}".strip() if mode == "ferry" else f"{mode} {hm}".strip())
    chain = " + ".join(p for p in parts if p)
    cost = o.get("cost_eur")
    cost_str = f", ~€{cost:.0f}" if cost is not None else ""
    trip = "rt" if o.get("round_trip") else "ow"
    suffix = f", then {chain} to {o.get('name')}{cost_str} {trip}"
    if o.get("marginal"):
        note = str(o.get("note") or "").strip()
        head = note.split(".")[0].strip() if note else ""
        if head:
            suffix += f" — marginal: {head}"
    return suffix


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
    connection: Optional[Dict[str, Any]] = None,
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
    # S5 self-transfer is two SEPARATE tickets through a hub, so a single
    # origin->destination booking link would be a lie (there is no direct
    # flight). Emit no combined link — the four segments live in ``legs`` and
    # the hub in ``connection`` (CONTRACT §2b). Every other shape keeps its link.
    links = {} if shape == "S5" else _links(carriers, origin, destination, out_date, return_date)
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
        "links": links,
    }
    if connection is not None:
        deal["connection"] = connection
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


def via_hub_why(price_eur: float, connection: Dict[str, Any]) -> str:
    """The S5 self-transfer ``why`` (Task 16). ALWAYS carries the separate-tickets
    disclosure (a missed connection is the traveller's risk) and, when set, the
    displayed self-transfer buffer — both are load-bearing honesty, never
    dropped. Connection gaps are the VERIFIED ones."""
    hub = connection.get("hub", "?")
    co = _fmt_hm(connection.get("connect_out_minutes")) or "?"
    cr = _fmt_hm(connection.get("connect_ret_minutes")) or "?"
    buf = connection.get("buffer_eur")
    buf_str = f", incl. ~€{buf:.0f} self-transfer buffer" if buf else ""
    return (
        f"€{price_eur:.0f} round-trip via {hub} — 2 SEPARATE tickets, self-transfer "
        f"(missed connection is your risk; {co}/{cr} connections, ≥3h enforced){buf_str}"
    )


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
    # A gem-extended deal names the gem ("Halki (via RHO)") rather than the bare
    # gateway IATA, so the paste-ready summary reads honestly (Task 15).
    where = cheapest.get("destination_display") or cheapest["destination"]
    # An S5 self-transfer must ALWAYS disclose the separate-tickets risk in the
    # paste-ready summary too, not only in the deal's why (Task 16).
    s5_note = ""
    if cheapest.get("shape") == "S5":
        hub = (cheapest.get("connection") or {}).get("hub", "a hub")
        s5_note = f" [self-transfer via {hub}, 2 separate tickets]"
    return (
        f"Found {n} {plural} from {origin_str}, cheapest {where} "
        f"€{cheapest['price_eur']:.0f} {trip} {dates} ({conf})"
    ) + s5_note + _coverage_gap_note(sources)


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


def _html_escape(s: str) -> str:
    """Escape the three characters Telegram's HTML parse mode reserves."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram_text(env: Dict[str, Any], *, html: bool = False) -> str:
    """The digest string built from the SAME envelope (Global Constraint 2 /
    UPGRADE-PLAN §6 — one renderer, no second data path; Task 8's notifier sends
    exactly this).

    ``html=False`` -> plain text. ``html=True`` -> Telegram HTML parse-mode
    markup: the summary is bolded, each deal line carries a ``<a href>`` deep
    link (preserved from the envelope's ``links``) so a URL with an unescaped
    ``_`` can't silently 400 the way Markdown parse mode did (UPGRADE-PLAN §6).
    Reserved characters are escaped in HTML mode."""
    esc = _html_escape if html else (lambda s: s)
    summary = env.get("summary", "")
    lines = [f"<b>{esc(summary)}</b>" if html else summary]
    for i, d in enumerate(env.get("results", [])[:10], 1):
        dates = d["out_date"] + (f"–{d['return_date']}" if d.get("return_date") else "")
        conf = "" if d["price_confidence"] == "exact" else " ~"
        route = f"{d['origin']}→{d['destination']}"
        body = f"{i}. {route} €{d['price_eur']:.0f}{conf} {dates}"
        if html:
            link = next(iter((d.get("links") or {}).values()), None)
            body = esc(body)
            if link:
                body = f'{body} · <a href="{esc(link)}">book</a>'
        lines.append(body)
    return "\n".join(lines)
