"""The deterministic query compiler (SEARCH-DESIGN §4, CONTRACT §6).

``compile_plan(spec)`` is **pure** — no network, no wall-clock reads at all —
and turns a ``SearchSpec`` into a typed, inspectable ``CallPlan`` (the ``plan``
command prints exactly this). ``Planner.execute(plan, spec)`` runs the plan
under the shared rate limiter and returns raw results + a per-source status,
which ``output.py`` renders into the frozen envelope (the ``run`` command).

Task 6 scope: the ``direct`` shape as a **round-trip** (S2). RT-ANYWHERE on
Ryanair enumerates every served destination in one call (exact fares); Wizz TT
adds approximate cover per where-matched destination. Other shapes and one-way
are refused politely (they arrive in Tasks 7/10). The execute loop runs on the
process-wide worker pool (``http.get_executor``) so per-thread sessions never
leak across searches (Task 3 review carry-over).
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flight_deals import http, output
from flight_deals.config import get_config
from flight_deals.models import DayFare, FareLeg, FarePair
from flight_deals.orchestrator import aggregate_status, status_for_exception
from flight_deals.registry.destinations import DestinationRegistry
from flight_deals.registry.ground_matrix import GROUND_MODE

logger = logging.getLogger(__name__)

# Nominal rate for the (deterministic) time estimate. The live token bucket's
# rate is mutated by config wiring and by tests (conftest sets it to 1e6), so
# ``estimated_seconds`` is computed from this stable constant instead — Global
# Constraint 9's default ~1 req/s — keeping ``plan`` output byte-stable.
NOMINAL_RATE = 1.0

DEFAULT_MAX_CALLS = 40

# S4 open-jaw pair cap (Task 11 req 4): with the computed ground matrix a large
# where-expression can match many open-jaw pairs. We consider only the 40
# SHORTEST-ground pairs among the where-matched airports per run, and the plan
# output reports how many were dropped (no silent truncation). CAL descriptors
# are still deduped by (origin, destination, month), so the actual call count
# stays well under 2×matched airports + the direct-shape calls.
S4_PAIR_CAP = 40


def _capped_openjaw_pairs(registry, matched_set, cap: int = S4_PAIR_CAP):
    """The where-matched open-jaw pairs (both airports in ``matched_set``),
    sorted shortest-ground first and capped at ``cap``. Returns
    ``(kept_pairs, dropped_count)``. Shared by ``compile_plan`` (which builds
    CAL descriptors) and ``execute``'s ``_build_openjaw`` (which pairs the CAL
    minima) so the two can never disagree on which pairs are in play."""
    pairs = [
        p for p in registry.get_open_jaw_pairs()
        if str(p["a"]).upper() in matched_set and str(p["b"]).upper() in matched_set
    ]
    pairs.sort(key=lambda p: (int(p.get("ground_minutes") or 0),
                              str(p["a"]).upper(), str(p["b"]).upper()))
    kept = pairs[:cap]
    return kept, len(pairs) - len(kept)

# Estimate->confirm margin band (Task 7 quality fix): budget/top-N truncation
# below is computed on *estimates*, so a deal confirm() could rescue (or that
# could back-fill a truncated slot) must survive past that cut in order to be
# confirmable at all. ``confirm_band`` is a deliberately small superset of the
# final display set: BUDGET_MARGIN_FACTOR widens the budget ceiling so a deal
# estimated up to 20% over budget is still confirmed (its exact price may come
# in under budget), and the rank cutoff is extended by a bounded extra count so
# a deal estimated just outside the top-N can still confirm and back-fill a
# cheaper slot. The bound keeps confirm's call count honest — see
# ``_confirm_band_size``.
BUDGET_MARGIN_FACTOR = 1.20


def _confirm_band_size(max_results: int) -> int:
    """Extra candidates (beyond ``max_results``) kept for the confirm margin
    band: ``min(5, ceil(max_results * 0.5))`` extra slots, e.g. 5 extra at
    max_results=10, 2 extra at max_results=4. Bounded so confirm's fan-out
    never grows unboundedly with the candidate pool."""
    if max_results <= 0:
        return 0
    extra = min(5, math.ceil(max_results * 0.5))
    return max_results + extra


# --------------------------------------------------------------------------- #
# Refusals                                                                     #
# --------------------------------------------------------------------------- #
class PlannerRefusal(Exception):
    """The planner declines a spec it *understood* but won't run (unsupported
    shape, one-way not yet enabled, too many calls). Carries a ``hint`` — the
    CLI maps it to the exit-2 envelope, same as a spec error."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# Typed call descriptors (CONTRACT §6)                                         #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CallDescriptor:
    provider: str            # "ryanair" | "wizzair"
    endpoint: str            # "roundTripFares" | "cheapestPerDay" | "timetable"
    mode: str                # "anywhere" | "exact" | "calendar" | "timetable"
    shape: str               # "S1".."S5"
    params: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "mode": self.mode,
            "shape": self.shape,
            "params": dict(self.params),
        }

    # Deterministic sort key so a compiled plan is byte-stable.
    def sort_key(self) -> Tuple:
        p = self.params
        return (
            0 if self.mode == "anywhere" else 1,
            self.provider,
            self.endpoint,
            self.mode,
            p.get("origin", ""),
            p.get("destination", ""),
            str(p.get("month", "")),
        )


@dataclass
class CallPlan:
    calls: List[CallDescriptor] = field(default_factory=list)
    estimated_calls: int = 0
    estimated_seconds: float = 0.0
    # S4 open-jaw pair accounting (Task 11) — only set when the open-jaw shape
    # is compiled, so non-open-jaw plans stay byte-identical.
    openjaw_pairs_considered: Optional[int] = None
    openjaw_pairs_dropped: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "calls": [c.to_dict() for c in self.calls],
            "estimated_calls": self.estimated_calls,
            "estimated_seconds": self.estimated_seconds,
        }
        # Additive, present only for open-jaw plans (no silent truncation: a
        # capped run reports exactly how many matched pairs it dropped).
        if self.openjaw_pairs_considered is not None:
            d["openjaw_pairs_considered"] = self.openjaw_pairs_considered
            d["openjaw_pairs_dropped"] = self.openjaw_pairs_dropped
        return d


# --------------------------------------------------------------------------- #
# compile (PURE)                                                               #
# --------------------------------------------------------------------------- #
def _matched_destinations(spec, registry: DestinationRegistry) -> List[str]:
    """Where-matched destination IATAs (static tags only — no network, so
    ``compile`` stays pure), sorted, with the origins removed."""
    origins = {o.upper() for o in spec.origins}
    if spec.where:
        airports = registry.matching(spec.where)  # raises WhereParseError on bad expr
        matched = {a.iata for a in airports}
    else:
        matched = {a.iata for a in registry.airports}
    # A route watch pins one (or a few) specific destinations (SavedSearch,
    # Task 8): intersect so RT-ANYWHERE results and the TT fan-out are both
    # restricted to exactly those routes. ``destinations=None`` keeps the
    # existing category behaviour byte-identical.
    dests = getattr(spec, "destinations", None)
    if dests:
        matched &= {d.upper() for d in dests}
    return sorted(matched - origins)


class WhereGateResult:
    """Outcome of :func:`check_where_gate` — a pre-network sanity check on
    ``spec.where``. Either the caller must stop right now (``stop=True``, with
    ``env``/``exit_code`` already built — no plan is ever compiled/executed)
    or it should continue, optionally carrying ``unknown_tags``/``hint`` to
    attach to the eventual result envelope (the partial-match case)."""

    __slots__ = ("stop", "env", "exit_code", "unknown_tags", "hint")

    def __init__(self, *, stop: bool = False, env: Optional[Dict[str, Any]] = None,
                 exit_code: int = 0, unknown_tags: Optional[List[str]] = None,
                 hint: Optional[str] = None):
        self.stop = stop
        self.env = env
        self.exit_code = exit_code
        self.unknown_tags = unknown_tags or []
        self.hint = hint


def check_where_gate(spec, registry: DestinationRegistry) -> WhereGateResult:
    """Pre-network sanity check for ``spec.where`` (review item: a typo'd tag
    like ``"seasid & italy"`` used to sail through ``getaway``/``oneway``/
    ``run`` all the way to a live Ryanair RT-ANYWHERE call before reporting
    ``no_service`` — burning a call over a where-expression that could never
    possibly match). Called after ``compile``-equivalent resolution but before
    any provider is touched.

    Three outcomes, matching ``where show``'s spirit but keyed off the
    ACTUAL destination count (not just "were all identifiers unknown"):

    * unknown tag(s) AND zero matched destinations -> ``stop`` with an exit-2
      envelope (did-you-mean hint) — a typo masquerading as an empty category.
    * unknown tag(s) but destinations still resolve (partial, e.g.
      ``"seasid | italy"``) -> continue; ``unknown_tags``/``hint`` are
      returned for the caller to attach to the final envelope.
    * zero matched destinations with NO unknown tags (a legitimately empty
      category, e.g. ``"ski"`` with no BUD-reachable ski airports) -> ``stop``
      with an exit-0 ``no_match`` envelope — running a plan over zero
      destinations would only waste calls for a guaranteed-empty result.
    """
    if not getattr(spec, "where", None):
        return WhereGateResult()

    unknown = registry.unknown_tags(spec.where)
    matched = _matched_destinations(spec, registry)
    if matched:
        if unknown:
            return WhereGateResult(unknown_tags=unknown, hint=registry.tag_hint(unknown))
        return WhereGateResult()

    if unknown:
        hint = registry.tag_hint(unknown)
        env = output.error_envelope(
            f"--where {spec.where!r} matched no destinations — unknown tag(s): {', '.join(unknown)}",
            hint,
        )
        return WhereGateResult(stop=True, env=env, exit_code=2)

    env = output.envelope(
        results=[],
        summary=f"no destinations match --where {spec.where!r} — see 'flight-deals where list' "
                "for available tags",
        sources={},
        next=["flight-deals where list"],
        route_status="no_match",
    )
    return WhereGateResult(stop=True, env=env, exit_code=0)


def compile_plan(spec, registry: Optional[DestinationRegistry] = None, *, rate: float = NOMINAL_RATE) -> CallPlan:
    """Compile a ``SearchSpec`` into a ``CallPlan``. Pure: no network, no wall
    clock. Refuses (with a hint) any shape/one-way not enabled in Task 6.

    Note on ``depart.kind == "dates"`` (a comma list of specific outbound
    dates): the compiled calls still request the *window* spanning those
    dates (``out_from``..``out_to`` for RT-ANYWHERE/RT-EXACT, and the TT
    date range) — narrowing the request itself isn't worth a call per date.
    The plan's ``params`` therefore reflect the request window, not the
    exact list. It is ``execute()`` that filters candidate results back down
    to exactly the listed dates (see ``_pair_timetable`` and the ryanair
    branch below) — a listed date list must never silently widen to its
    span."""
    registry = registry or DestinationRegistry()

    # Enabled shapes: direct (S1/S2), extended-origin (S3), open-jaw (S4).
    # via-hub (S5) stays refused — it needs the time-verification funnel (Task
    # 5c / SEARCH-DESIGN §2, S5) that isn't built.
    refused = [s for s in spec.shapes if s == "via-hub"]
    if refused:
        raise PlannerRefusal(
            "shape via-hub not yet enabled",
            'via-hub (self-transfer) is not enabled yet; drop it from "shapes" '
            '(use e.g. "shapes":["direct","extended-origin","open-jaw"])',
        )
    depart = spec.depart_spec
    round_trip = spec.is_round_trip
    shape = "S2" if round_trip else "S1"
    if round_trip:
        lo, hi = spec.nights_range
        # TT must cover outbound window AND the latest possible return (out_to +
        # max nights). Rows come back un-clipped (Task 4 note) — execute() clips.
        tt_from = depart.out_from
        tt_to = (date.fromisoformat(depart.out_to) + timedelta(days=hi)).isoformat()
    else:
        # One-way (S1, Task 7): outbound window only, no return leg.
        lo = hi = None
        tt_from, tt_to = depart.out_from, depart.out_to

    matched = _matched_destinations(spec, registry)
    want_ryanair = "ryanair" in spec.carriers
    want_wizz = "wizzair" in spec.carriers

    calls: List[CallDescriptor] = []
    for origin in sorted({o.upper() for o in spec.origins}):
        if want_ryanair:
            if round_trip:
                # RT-ANYWHERE: every Ryanair destination in ONE call (exact).
                calls.append(CallDescriptor(
                    provider="ryanair", endpoint="roundTripFares", mode="anywhere", shape="S2",
                    params={
                        "origin": origin,
                        "out_from": depart.out_from,
                        "out_to": depart.out_to,
                        "duration_from": lo,
                        "duration_to": hi,
                    },
                ))
            else:
                # OW-ANYWHERE: every Ryanair destination in ONE call (exact).
                calls.append(CallDescriptor(
                    provider="ryanair", endpoint="oneWayFares", mode="anywhere", shape="S1",
                    params={
                        "origin": origin,
                        "out_from": depart.out_from,
                        "out_to": depart.out_to,
                    },
                ))
        if want_wizz:
            # TT per where-matched destination. The registry can't know which
            # routes Wizz actually serves (no public route endpoint — Task 5),
            # so we plan a TT for every matched destination as an honest upper
            # bound; execute() tolerates no_service on the ones Wizz doesn't fly.
            for dest in matched:
                calls.append(CallDescriptor(
                    provider="wizzair", endpoint="timetable", mode="timetable", shape=shape,
                    params={
                        "origin": origin,
                        "destination": dest,
                        "date_from": tt_from,
                        "date_to": tt_to,
                    },
                ))

    shapes = set(spec.shapes)
    origins = sorted({o.upper() for o in spec.origins})

    # ------------------------------------------------------------------ #
    # S3 extended-origin (round-trip only): a Ryanair RT-ANYWHERE sweep   #
    # from each ground-reachable extended origin (VIE/BTS from BUD).      #
    # ------------------------------------------------------------------ #
    # Per SEARCH-DESIGN §4's call-plan example, S3 is FR RT-ANYWHERE ONLY
    # (no Wizz TT from the extended origin) — one call per extended origin,
    # keeping the sweep cheap. Wizz-from-extended-origin is a future refinement.
    if "extended-origin" in shapes and round_trip and want_ryanair:
        for base in origins:
            for via, info in sorted(registry.origin_ground.items()):
                if str(info.get("from", "")).upper() != base:
                    continue
                calls.append(CallDescriptor(
                    provider="ryanair", endpoint="roundTripFares", mode="anywhere", shape="S3",
                    params={
                        "origin": via,
                        "base_origin": base,
                        "out_from": depart.out_from,
                        "out_to": depart.out_to,
                        "duration_from": lo,
                        "duration_to": hi,
                    },
                ))

    # ------------------------------------------------------------------ #
    # S4 open-jaw (round-trip only): Ryanair CAL for each open-jaw pair   #
    # whose BOTH airports are in the where-matched set. Fly O->D1, ground #
    # D1->D2, fly D2->O — both directions of each pair are considered, so #
    # we need CAL(O->D1), CAL(O->D2) (outbound months) and CAL(D1->O),    #
    # CAL(D2->O) (return months). Descriptors deduped by (origin,dest,month).#
    # ------------------------------------------------------------------ #
    openjaw_considered: Optional[int] = None
    openjaw_dropped: Optional[int] = None
    if "open-jaw" in shapes and round_trip and want_ryanair:
        matched_set = set(matched)
        kept_pairs, openjaw_dropped = _capped_openjaw_pairs(registry, matched_set)
        openjaw_considered = len(kept_pairs)
        out_months = _months_spanning(depart.out_from, depart.out_to)
        ret_months = _months_spanning(
            (date.fromisoformat(depart.out_from) + timedelta(days=lo)).isoformat(),
            (date.fromisoformat(depart.out_to) + timedelta(days=hi)).isoformat(),
        )
        cal_needed: set = set()
        for base in origins:
            for pair in kept_pairs:
                a, b = str(pair["a"]).upper(), str(pair["b"]).upper()
                for d1, d2 in ((a, b), (b, a)):
                    for m in out_months:
                        cal_needed.add((base, d1, m))   # outbound O -> D1
                    for m in ret_months:
                        cal_needed.add((d2, base, m))   # inbound  D2 -> O
        for (o, d, m) in sorted(cal_needed):
            calls.append(CallDescriptor(
                provider="ryanair", endpoint="cheapestPerDay", mode="calendar", shape="S4",
                params={"origin": o, "destination": d, "month": m},
            ))

    calls.sort(key=lambda c: c.sort_key())
    n = len(calls)
    return CallPlan(
        calls=calls, estimated_calls=n, estimated_seconds=round(n / rate, 1),
        openjaw_pairs_considered=openjaw_considered,
        openjaw_pairs_dropped=openjaw_dropped,
    )


def _months_spanning(start_iso: str, end_iso: str) -> List[str]:
    """The ``YYYY-MM`` months (inclusive) spanned by ``[start_iso, end_iso]``."""
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    months, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return months


def _round_up_to_5(n: int) -> int:
    """Round ``n`` up to the next multiple of 5 (a friendlier --max-calls
    suggestion than the raw estimate, e.g. 41 -> 45)."""
    return -(-n // 5) * 5


def check_max_calls(plan: CallPlan, max_calls: int) -> None:
    """Refuse a plan whose call count exceeds ``max_calls`` (default 40).

    The hint is a single exact corrected command — the same invocation with
    ``--max-calls`` raised to the estimate rounded up to the next 5 — plus one
    trailing "or narrow --where" clause. (Review item: the old 3-option prose
    hint tripped on the skill's own worked example; a single copy-pasteable
    correction is worth more than a menu.)"""
    if plan.estimated_calls > max_calls:
        suggested = _round_up_to_5(plan.estimated_calls)
        raise PlannerRefusal(
            f"plan needs {plan.estimated_calls} calls, over the --max-calls {max_calls} cap",
            f"re-run with --max-calls {suggested}, or narrow --where",
        )


# --------------------------------------------------------------------------- #
# execute                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class _Candidate:
    """One best trip per (origin, destination), pre-render. Exact from a Ryanair
    FarePair (S2) / DayFare (S1); approximate from a paired/one-way Wizz
    timetable estimate. ``return_date``/``nights`` are ``None`` for one-way."""
    origin: str
    destination: str
    out_date: str
    return_date: Optional[str]
    nights: Optional[int]
    price_eur: float
    price_confidence: str
    carriers: List[str]
    legs: List[Dict[str, Any]]
    shape: str = "S2"
    ground: Optional[Dict[str, Any]] = None
    # Dedup key override (S4 open-jaw). ``None`` -> ("point", origin, destination):
    # S1/S2/S3 to the same destination compete (cheapest wins, so an extended
    # origin surfaces only when it genuinely beats direct). S4 is a distinct
    # two-city product keyed by the unordered airport pair.
    dedup: Optional[Tuple] = None

    def __post_init__(self):
        # Regression guard (review item: a live Wizz approximate deal showed
        # destination=BGY while its legs referenced MXP — a multi-airport
        # metro substitution, root-caused in _pair_timetable). For direct
        # point-to-point shapes (S1/S2) the trip's `destination` must always
        # be the airport the outbound leg actually flies to; S3 (extended-
        # origin ground+fly) and S4 (open-jaw fly-in/fly-home) intentionally
        # differ — their ground legs live inside `legs` by design.
        if self.shape in ("S1", "S2") and self.legs:
            leg_dest = self.legs[0].get("destination")
            if leg_dest and leg_dest != self.destination:
                raise ValueError(
                    f"candidate destination {self.destination!r} does not match the "
                    f"outbound leg's actual airport {leg_dest!r} (shape {self.shape})"
                )

    def rank_key(self) -> Tuple:
        # cheapest wins; on a price tie an exact fare beats an approximate one;
        # then carriers string for a fully deterministic order.
        approx = self.price_confidence != "exact"
        return (self.price_eur, approx, "+".join(self.carriers))

    def dedup_key(self) -> Tuple:
        return self.dedup if self.dedup is not None else ("point", self.origin, self.destination)


def _dayfare_to_candidate(df: DayFare, carrier: str, confidence: str) -> _Candidate:
    """One-way (S1) candidate from a single outbound DayFare."""
    return _Candidate(
        origin=df.origin,
        destination=df.destination,
        out_date=df.date,
        return_date=None,
        nights=None,
        price_eur=round(df.price_eur, 2),
        price_confidence=confidence,
        carriers=[carrier],
        shape="S1",
        legs=[
            output.flight_leg(
                df.origin, df.destination, carrier, df.date, df.price_eur,
                departure_time=df.departure_time, flight_number=df.flight_number,
            ),
        ],
    )


def _farepair_to_candidate(fp: FarePair) -> _Candidate:
    return _Candidate(
        origin=fp.origin,
        destination=fp.destination,
        out_date=fp.out_date,
        return_date=fp.return_date,
        nights=fp.nights,
        price_eur=round(fp.total_price_eur, 2),
        price_confidence="exact",
        carriers=["ryanair"],
        shape="S2",
        legs=[
            output.flight_leg(
                fp.outbound.origin, fp.outbound.destination, "ryanair", fp.outbound.date,
                fp.outbound.price_eur, departure_time=fp.outbound.departure_time,
                flight_number=fp.outbound.flight_number, duration_minutes=fp.outbound.duration_minutes,
            ),
            output.flight_leg(
                fp.inbound.origin, fp.inbound.destination, "ryanair", fp.inbound.date,
                fp.inbound.price_eur, departure_time=fp.inbound.departure_time,
                flight_number=fp.inbound.flight_number, duration_minutes=fp.inbound.duration_minutes,
            ),
        ],
    )


def _extended_origin_candidate(fp: FarePair, base_origin: str, ground: Dict[str, Any]) -> _Candidate:
    """S3 extended-origin: a Ryanair RT-ANYWHERE pair from the extended origin
    (VIE/BTS) wrapped with the round-trip ground leg to/from the base origin.
    The trip's endpoints are ``base_origin``/``destination``; the flown airport
    (VIE) appears only inside ``legs``. Total = fare + 2×ground cost (out+back)."""
    via = fp.origin  # e.g. VIE
    g_min = int(ground.get("minutes") or 0)
    g_cost = float(ground.get("est_cost_eur") or 0.0)
    g_mode = ground.get("mode", "bus")
    total = round(fp.total_price_eur + 2 * g_cost, 2)
    legs = [
        output.ground_leg(base_origin, via, g_mode, g_min, cost_eur=g_cost),
        output.flight_leg(
            fp.outbound.origin, fp.outbound.destination, "ryanair", fp.outbound.date,
            fp.outbound.price_eur, departure_time=fp.outbound.departure_time,
            flight_number=fp.outbound.flight_number, duration_minutes=fp.outbound.duration_minutes,
        ),
        output.flight_leg(
            fp.inbound.origin, fp.inbound.destination, "ryanair", fp.inbound.date,
            fp.inbound.price_eur, departure_time=fp.inbound.departure_time,
            flight_number=fp.inbound.flight_number, duration_minutes=fp.inbound.duration_minutes,
        ),
        output.ground_leg(via, base_origin, g_mode, g_min, cost_eur=g_cost),
    ]
    return _Candidate(
        origin=base_origin,
        destination=fp.destination,
        out_date=fp.out_date,
        return_date=fp.return_date,
        nights=fp.nights,
        price_eur=total,
        price_confidence="exact",
        carriers=["ryanair"],
        shape="S3",
        legs=legs,
        # Extended-origin ground (VIE/BTS) is hand-curated registry data.
        ground=output.ground_summary(2 * g_min, round(2 * g_cost, 2), g_mode,
                                     estimate_basis="curated"),
    )


def _openjaw_candidate(
    base: str, d1: str, d2: str, out_fare: DayFare, ret_fare: DayFare, nights: int,
    ground_minutes: int, ground_cost: float, ground_mode: str,
    estimate_basis: Optional[str] = None, ground_distance_km: Optional[float] = None,
    has_ferry: Optional[bool] = None,
) -> _Candidate:
    """S4 open-jaw: fly ``base->d1``, ground ``d1->d2``, fly ``d2->base``. Two
    exact one-way Ryanair legs (CAL) + one ground hop. The trip's ``destination``
    is the fly-in airport ``d1``; the fly-home airport ``d2`` and the hop live in
    ``legs``. Total = leg1 + leg2 + ground cost (one hop, not doubled).
    ``ground_distance_km`` is the pair's routed road distance (``km_road``) when
    the hop is a computed matrix pair; curated pairs carry no ``km_road`` and
    stay ``None`` (the field is nullable — CONTRACT §2a)."""
    total = round(out_fare.price_eur + ret_fare.price_eur + ground_cost, 2)
    legs = [
        output.flight_leg(base, d1, "ryanair", out_fare.date, out_fare.price_eur,
                          departure_time=out_fare.departure_time),
        output.ground_leg(d1, d2, ground_mode, ground_minutes, cost_eur=ground_cost,
                          distance_km=ground_distance_km),
        output.flight_leg(d2, base, "ryanair", ret_fare.date, ret_fare.price_eur,
                          departure_time=ret_fare.departure_time),
    ]
    return _Candidate(
        origin=base,
        destination=d1,
        out_date=out_fare.date,
        return_date=ret_fare.date,
        nights=nights,
        price_eur=total,
        price_confidence="exact",
        carriers=["ryanair"],
        shape="S4",
        legs=legs,
        ground=output.ground_summary(ground_minutes, ground_cost, ground_mode,
                                     estimate_basis=estimate_basis,
                                     has_ferry=has_ferry),
        dedup=("openjaw", base, frozenset({d1, d2})),
    )


def _pair_timetable(
    origin: str, dest: str, out_fares: List[DayFare], ret_fares: List[DayFare],
    depart, nights_range: Tuple[int, int],
) -> Optional[_Candidate]:
    """Window-clip the (un-clipped) TT rows and pair the cheapest in-window
    outbound with the cheapest in-window inbound whose gap is within the nights
    range -> one approximate round-trip estimate for this destination.

    When ``depart.kind == "dates"`` (an explicit comma list), the outbound
    candidates are additionally filtered down to exactly those dates — the
    window is only ever the *request* span (``out_from``..``out_to``); a
    date list must not silently widen to every date in between.

    A round trip is only ever paired between rows that land at and depart
    from the SAME physical airport (``o.destination == r.origin``): a
    multi-airport metro area (e.g. Milan MXP/BGY, London STN/LTN/LGW) can have
    the API substitute a sibling member for the requested ``dest`` (review
    item — a live deal showed destination=BGY with legs referencing MXP), and
    rows must never be Frankenstein-paired across two different airports. The
    resulting candidate's ``destination`` is ALWAYS the row's actual airport,
    never the externally-requested ``dest`` — so it can never diverge from
    its own legs."""
    lo, hi = nights_range
    o_from = date.fromisoformat(depart.out_from)
    o_to = date.fromisoformat(depart.out_to)
    outs = [f for f in out_fares if o_from <= date.fromisoformat(f.date) <= o_to]
    if depart.kind == "dates":
        allowed = set(depart.dates)
        outs = [f for f in outs if f.date in allowed]
    r_lo, r_hi = o_from + timedelta(days=lo), o_to + timedelta(days=hi)
    rets = [f for f in ret_fares if r_lo <= date.fromisoformat(f.date) <= r_hi]

    best: Optional[Tuple[float, DayFare, DayFare, int]] = None
    for o in outs:
        od = date.fromisoformat(o.date)
        for r in rets:
            if o.destination != r.origin:
                continue  # never pair two different physical airports
            n = (date.fromisoformat(r.date) - od).days
            if lo <= n <= hi:
                total = round(o.price_eur + r.price_eur, 2)
                if best is None or total < best[0]:
                    best = (total, o, r, n)
    if best is None:
        return None
    total, o, r, n = best
    if o.destination != dest:
        logger.warning(
            "planner: wizz TT %s->%s returned fares for %s instead (multi-airport "
            "metro substitution?) — labelling the deal with the actual airport",
            origin, dest, o.destination,
        )
    return _Candidate(
        origin=origin,
        destination=o.destination,
        out_date=o.date,
        return_date=r.date,
        nights=n,
        price_eur=total,
        price_confidence="approximate",
        carriers=["wizzair"],
        legs=[
            output.flight_leg(o.origin, o.destination, "wizzair", o.date,
                              o.price_eur, departure_time=o.departure_time),
            output.flight_leg(r.origin, r.destination, "wizzair", r.date,
                              r.price_eur, departure_time=r.departure_time),
        ],
    )


def _cheapest_oneway_timetable(
    origin: str, dest: str, out_fares: List[DayFare], depart,
) -> Optional[_Candidate]:
    """Cheapest in-window outbound Wizz fare -> one approximate one-way (S1)
    estimate. Window-clips the un-clipped TT rows the same way ``_pair_timetable``
    does, and honours an explicit ``dates`` list."""
    o_from = date.fromisoformat(depart.out_from)
    o_to = date.fromisoformat(depart.out_to)
    outs = [f for f in out_fares if o_from <= date.fromisoformat(f.date) <= o_to]
    if depart.kind == "dates":
        allowed = set(depart.dates)
        outs = [f for f in outs if f.date in allowed]
    if not outs:
        return None
    best = min(outs, key=lambda f: f.price_eur)
    return _dayfare_to_candidate(best, "wizzair", "approximate")


class Planner:
    """Compiles + executes specs. Provider instances are attributes so tests can
    monkeypatch them; the shared worker pool keeps sessions from leaking."""

    def __init__(self, registry=None, ryanair=None, wizz=None, config=None):
        from flight_deals.providers.ryanair import RyanairProvider
        from flight_deals.providers.wizz import WizzProvider

        self.config = config or get_config()
        self.registry = registry or DestinationRegistry()
        self.ryanair = ryanair or RyanairProvider()
        self.wizz = wizz or WizzProvider()
        # Align the shared limiter with configured policy (Constraint 9).
        http.set_rate(self.config.http_rate_per_second)

    def compile(self, spec) -> CallPlan:
        return compile_plan(spec, self.registry)

    # -- per-call workers (each returns (kind, payload, event)) ------------- #
    def _run_anywhere(self, call: CallDescriptor, fresh: bool):
        p = call.params
        try:
            pairs = self.ryanair.roundtrip_fares(
                p["origin"], dest=None,
                out_from=p["out_from"], out_to=p["out_to"],
                duration_from=p["duration_from"], duration_to=p["duration_to"],
                use_cache=not fresh,
            )
            return ("ryanair", pairs, {"provider": "ryanair", "status": "ok"})
        except Exception as e:  # typed provider errors -> frozen status
            logger.warning("planner: ryanair anywhere %s failed: %s", p.get("origin"), e)
            return ("ryanair", [], {"provider": "ryanair", "status": status_for_exception(e), "detail": str(e)})

    def _run_oneway_anywhere(self, call: CallDescriptor, fresh: bool):
        p = call.params
        try:
            fares = self.ryanair.oneway_fares(
                p["origin"], dest=None,
                out_from=p["out_from"], out_to=p["out_to"], use_cache=not fresh,
            )
            return ("ryanair_ow", fares, {"provider": "ryanair", "status": "ok"})
        except Exception as e:
            logger.warning("planner: ryanair oneway %s failed: %s", p.get("origin"), e)
            return ("ryanair_ow", [], {"provider": "ryanair", "status": status_for_exception(e), "detail": str(e)})

    def _run_timetable(self, call: CallDescriptor, spec, fresh: bool):
        p = call.params
        try:
            out_fares, ret_fares = self.wizz.timetable(
                p["origin"], p["destination"], p["date_from"], p["date_to"], use_cache=not fresh,
            )
            if spec.is_round_trip:
                cand = _pair_timetable(
                    p["origin"], p["destination"], out_fares, ret_fares,
                    spec.depart_spec, spec.nights_range,
                )
            else:
                cand = _cheapest_oneway_timetable(
                    p["origin"], p["destination"], out_fares, spec.depart_spec,
                )
            return ("wizzair", cand, {"provider": "wizzair", "status": "ok"})
        except Exception as e:
            logger.warning("planner: wizz TT %s->%s failed: %s", p.get("origin"), p.get("destination"), e)
            return ("wizzair", None, {"provider": "wizzair", "status": status_for_exception(e), "detail": str(e)})

    def _run_calendar(self, call: CallDescriptor, fresh: bool):
        """Ryanair CAL (cheapestPerDay) for one route+month — the S4 open-jaw
        day-level enumeration. Returns exact per-day one-way minima."""
        p = call.params
        try:
            fares = self.ryanair.cheapest_per_day(
                p["origin"], p["destination"], p["month"], use_cache=not fresh,
            )
            return ("ryanair_cal", fares, {"provider": "ryanair", "status": "ok"})
        except Exception as e:
            logger.warning("planner: ryanair CAL %s->%s %s failed: %s",
                           p.get("origin"), p.get("destination"), p.get("month"), e)
            return ("ryanair_cal", [], {"provider": "ryanair", "status": status_for_exception(e), "detail": str(e)})

    def _build_openjaw(self, spec, matched, cal_fares):
        """Pair the CAL day-level minima into S4 open-jaw candidates: for each
        registry pair whose both airports are matched, take the cheapest
        (outbound O->D1, inbound D2->O) combo within the nights range across
        BOTH fly-in directions, add the D1<->D2 ground hop. One candidate per
        unordered pair (cheapest direction wins)."""
        lo, hi = spec.nights_range
        depart = spec.depart_spec
        o_from = date.fromisoformat(depart.out_from)
        o_to = date.fromisoformat(depart.out_to)
        allowed_dates = set(depart.dates) if depart.kind == "dates" else None
        # Same capped pair set the plan compiled CAL calls for (Task 11) — so we
        # never pair a dropped pair (which has no fetched fares anyway).
        kept_pairs, _dropped = _capped_openjaw_pairs(self.registry, matched)
        out: List[_Candidate] = []
        for base in sorted({o.upper() for o in spec.origins}):
            for pair in kept_pairs:
                a, b = str(pair["a"]).upper(), str(pair["b"]).upper()
                g_min = int(pair.get("ground_minutes") or 0)
                g_cost = float(pair.get("est_cost_eur") or 0.0)
                g_mode = pair.get("mode") or GROUND_MODE
                g_basis = pair.get("estimate_basis")
                g_km = pair.get("km_road")
                # Ferry disclosure (Task 12): an explicit has_ferry flag on the
                # pair, else inferred from a ferry mode string (curated corridors
                # may carry mode "ferry"/"ferry+ground" without the bool).
                g_ferry = pair.get("has_ferry")
                if g_ferry is None and "ferry" in str(g_mode).lower():
                    g_ferry = True
                best = None  # (total, d1, d2, out_fare, ret_fare, nights)
                for d1, d2 in ((a, b), (b, a)):
                    outs = [f for f in cal_fares.get((base, d1), [])
                            if o_from <= date.fromisoformat(f.date) <= o_to]
                    if allowed_dates is not None:
                        outs = [f for f in outs if f.date in allowed_dates]
                    rets = cal_fares.get((d2, base), [])
                    for of in outs:
                        od = date.fromisoformat(of.date)
                        for rf in rets:
                            n = (date.fromisoformat(rf.date) - od).days
                            if lo <= n <= hi:
                                total = round(of.price_eur + rf.price_eur + g_cost, 2)
                                if best is None or total < best[0]:
                                    best = (total, d1, d2, of, rf, n)
                if best is None:
                    continue
                _t, d1, d2, of, rf, n = best
                out.append(_openjaw_candidate(base, d1, d2, of, rf, n, g_min, g_cost, g_mode,
                                              estimate_basis=g_basis, ground_distance_km=g_km,
                                              has_ferry=g_ferry))
        return out

    def execute(self, plan: CallPlan, spec, *, fresh: bool = False) -> Dict[str, Any]:
        """Run the plan concurrently on the shared pool. Returns a dict with
        ``results`` (rendered Deal dicts, final budget+rank cut on estimates),
        ``confirm_band`` (a bounded superset of ``results`` — see
        ``_confirm_band_size`` — for intents.run_search's estimate->confirm
        rescue/back-fill pass), ``sources`` (frozen map), ``route_status``
        (None when non-empty), ``exit_code`` and ``candidate_count`` (pre-budget,
        for the empty-state distinction)."""
        matched = set(_matched_destinations(spec, self.registry))
        depart = spec.depart_spec
        # depart.kind == "dates" (an explicit comma list) must not silently
        # widen to the request window it spans (out_from..out_to) — the
        # RT-ANYWHERE/RT-EXACT FarePairs below are filtered the same way TT
        # rows are in _pair_timetable.
        allowed_dates = set(depart.dates) if depart.kind == "dates" else None
        executor = http.get_executor(self.config.max_workers)

        futures = {}
        for call in plan.calls:
            if call.mode == "anywhere" and call.endpoint == "roundTripFares":
                futures[executor.submit(self._run_anywhere, call, fresh)] = call
            elif call.mode == "anywhere" and call.endpoint == "oneWayFares":
                futures[executor.submit(self._run_oneway_anywhere, call, fresh)] = call
            elif call.mode == "timetable":
                futures[executor.submit(self._run_timetable, call, spec, fresh)] = call
            elif call.mode == "calendar":
                futures[executor.submit(self._run_calendar, call, fresh)] = call

        # best candidate per dedup key (see _Candidate.dedup_key)
        best: Dict[Tuple, _Candidate] = {}
        events: List[Dict[str, Any]] = []
        cal_fares: Dict[Tuple[str, str], List[DayFare]] = {}  # (origin,dest) -> DayFares (S4)

        def _offer(cand: Optional[_Candidate]):
            if cand is None:
                return
            key = cand.dedup_key()
            cur = best.get(key)
            if cur is None or cand.rank_key() < cur.rank_key():
                best[key] = cand

        for fut in as_completed(futures):
            call = futures[fut]
            kind, payload, event = fut.result()
            events.append(event)
            if kind == "ryanair":
                # RT-ANYWHERE pairs: S2 (direct) or S3 (from an extended origin).
                if call.shape == "S3":
                    ground = self.registry.get_origin_ground(call.params["origin"]) or {}
                    base = call.params.get("base_origin", "")
                    for fp in payload:
                        if fp.destination in matched and (
                            allowed_dates is None or fp.out_date in allowed_dates
                        ):
                            _offer(_extended_origin_candidate(fp, base, ground))
                else:
                    for fp in payload:  # FarePairs, already duration-filtered
                        if fp.destination in matched and (
                            allowed_dates is None or fp.out_date in allowed_dates
                        ):
                            _offer(_farepair_to_candidate(fp))
            elif kind == "ryanair_cal":
                for df in payload:
                    cal_fares.setdefault((df.origin, df.destination), []).append(df)
            elif kind == "ryanair_ow":
                # One-way DayFares (S1): clip to the outbound window + matched set.
                o_from = date.fromisoformat(depart.out_from)
                o_to = date.fromisoformat(depart.out_to)
                for df in payload:
                    if df.destination not in matched:
                        continue
                    if not (o_from <= date.fromisoformat(df.date) <= o_to):
                        continue
                    if allowed_dates is not None and df.date not in allowed_dates:
                        continue
                    _offer(_dayfare_to_candidate(df, "ryanair", "exact"))
            elif kind == "wizzair":
                # payload's destination is the ACTUAL fare airport (see
                # _pair_timetable/_cheapest_oneway_timetable) which can, in a
                # multi-airport metro substitution, diverge from the requested
                # call.params["destination"]. Guard it against the matched set
                # the same way the Ryanair branches above do — a substituted
                # airport outside the where-expression's matched set must
                # never surface as a deal (review item: destination/legs
                # mismatch, e.g. requested BGY, actual fare for MXP).
                if payload is not None and payload.destination not in matched:
                    logger.warning(
                        "planner: wizz TT %s->%s actual fare airport %s is outside "
                        "the matched destination set; dropping to avoid a "
                        "mislabeled/out-of-scope deal",
                        call.params.get("origin"), call.params.get("destination"), payload.destination,
                    )
                else:
                    _offer(payload)

        # S4 open-jaw: local pairing over the CAL day-level minima gathered above.
        if "open-jaw" in set(spec.shapes) and spec.is_round_trip and cal_fares:
            for cand in self._build_openjaw(spec, matched, cal_fares):
                _offer(cand)

        aggregated = aggregate_status(events)
        provider_failed = any(not v["ok"] for v in aggregated.values())

        candidates = list(best.values())
        candidate_count = len(candidates)

        # Confirm margin band: a wider (but bounded) superset of the final cut,
        # rendered so intents.confirm() can rescue/back-fill using confirmed
        # prices before the real budget+rank truncation below. Budget margin
        # only widens (never tightens) the strict filter; rank cutoff is
        # extended by a bounded extra (see _confirm_band_size).
        if spec.budget is not None:
            band_pool = [c for c in candidates if c.price_eur <= float(spec.budget) * BUDGET_MARGIN_FACTOR]
        else:
            band_pool = list(candidates)
        band_pool.sort(key=lambda c: (c.rank_key(), c.destination))
        band_pool = band_pool[: _confirm_band_size(spec.max_results)]
        confirm_band = [self._render(c) for c in band_pool]

        # budget filter
        if spec.budget is not None:
            candidates = [c for c in candidates if c.price_eur <= float(spec.budget)]

        candidates.sort(key=lambda c: (c.rank_key(), c.destination))
        candidates = candidates[: spec.max_results]

        results = [self._render(c) for c in candidates]

        route_status: Optional[str] = None
        exit_code = 0
        if not results:
            if provider_failed:
                route_status = "provider_error"  # untrustworthy emptiness -> exit 1
                exit_code = 1
            elif candidate_count == 0:
                route_status = "no_service"
            else:
                route_status = "no_match"  # had candidates, budget/constraints removed all

        return {
            "results": results,
            "confirm_band": confirm_band,
            "sources": output.project_sources(aggregated),
            "route_status": route_status,
            "exit_code": exit_code,
            "candidate_count": candidate_count,
        }

    @staticmethod
    def _render(c: _Candidate) -> Dict[str, Any]:
        round_trip = c.shape in ("S2", "S3", "S4")
        deal = output.build_deal(
            shape=c.shape,
            origin=c.origin,
            destination=c.destination,
            out_date=c.out_date,
            return_date=c.return_date,
            price_eur=c.price_eur,
            price_confidence=c.price_confidence,
            carriers=c.carriers,
            legs=c.legs,
            ground=c.ground,
            why=output.why_string(c.price_eur, c.price_confidence, round_trip=round_trip),
        )
        # Append the honest ground-transfer clause for shaped deals (S3/S4).
        deal["why"] = deal["why"] + output.ground_why_suffix(deal)
        return deal

    # -- one-shot: compile + guard + execute + build envelope --------------- #
    def run(self, spec, *, max_calls: int = DEFAULT_MAX_CALLS, fresh: bool = False) -> Tuple[Dict[str, Any], int]:
        # Where-gate (review item): never compile/execute a plan over a
        # where-expression that's guaranteed to match zero destinations — see
        # check_where_gate for the exit-2 (typo) vs exit-0 (legit empty
        # category) split.
        gate = check_where_gate(spec, self.registry)
        if gate.stop:
            return gate.env, gate.exit_code

        plan = self.compile(spec)
        check_max_calls(plan, max_calls)
        outcome = self.execute(plan, spec, fresh=fresh)
        # CONTRACT §3: exit 1 is ONLY reached when results == [] AND a needed
        # provider failed (route_status == "provider_error") — see execute()
        # above and the "Partial coverage" note (a provider failing while
        # another still produced results stays exit 0). error/hint are
        # therefore paired with exit 1 unconditionally here.
        error = hint = None
        if outcome["exit_code"] == 1:
            error = "provider_error"
            hint = "the next scheduled run will retry; or re-run with --fresh"
        env = output.envelope(
            results=outcome["results"],
            summary=output.build_summary(
                outcome["results"], spec.origins, outcome["route_status"], outcome["sources"],
            ),
            sources=outcome["sources"],
            next=output.build_next(spec, outcome["results"], outcome["route_status"]),
            route_status=outcome["route_status"],
            error=error,
            hint=hint,
        )
        if gate.unknown_tags:
            env["unknown_tags"] = gate.unknown_tags
            # CONTRACT §3: error/hint appear together or not at all — never
            # bolt a bare hint onto an exit-0 envelope.
            if env.get("error"):
                env["hint"] = f"{env['hint']}; also, {gate.hint}" if env.get("hint") else gate.hint
            else:
                env["summary"] = f"{env['summary']} (unknown tag(s) in --where: {gate.hint})"
        return env, outcome["exit_code"]
