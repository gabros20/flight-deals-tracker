"""The deterministic query compiler (SEARCH-DESIGN §4, CONTRACT §6).

``compile_plan(spec)`` is **pure** — no network, no clock reads beyond an
injectable ``today`` used only for validation — and turns a ``SearchSpec`` into
a typed, inspectable ``CallPlan`` (the ``plan`` command prints exactly this).
``Planner.execute(plan, spec)`` runs the plan under the shared rate limiter and
returns raw results + a per-source status, which ``output.py`` renders into the
frozen envelope (the ``run`` command).

Task 6 scope: the ``direct`` shape as a **round-trip** (S2). RT-ANYWHERE on
Ryanair enumerates every served destination in one call (exact fares); Wizz TT
adds approximate cover per where-matched destination. Other shapes and one-way
are refused politely (they arrive in Tasks 7/10). The execute loop runs on the
process-wide worker pool (``http.get_executor``) so per-thread sessions never
leak across searches (Task 3 review carry-over).
"""

from __future__ import annotations

import logging
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flight_deals import http, output
from flight_deals.config import get_config
from flight_deals.models import DayFare, FareLeg, FarePair
from flight_deals.orchestrator import aggregate_status, status_for_exception
from flight_deals.registry.destinations import DestinationRegistry

logger = logging.getLogger(__name__)

# Nominal rate for the (deterministic) time estimate. The live token bucket's
# rate is mutated by config wiring and by tests (conftest sets it to 1e6), so
# ``estimated_seconds`` is computed from this stable constant instead — Global
# Constraint 9's default ~1 req/s — keeping ``plan`` output byte-stable.
NOMINAL_RATE = 1.0

DEFAULT_MAX_CALLS = 40


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
            p.get("origin", ""),
            p.get("destination", ""),
        )


@dataclass
class CallPlan:
    calls: List[CallDescriptor] = field(default_factory=list)
    estimated_calls: int = 0
    estimated_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls": [c.to_dict() for c in self.calls],
            "estimated_calls": self.estimated_calls,
            "estimated_seconds": self.estimated_seconds,
        }


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
    return sorted(matched - origins)


def compile_plan(spec, registry: Optional[DestinationRegistry] = None, *, rate: float = NOMINAL_RATE) -> CallPlan:
    """Compile a ``SearchSpec`` into a ``CallPlan``. Pure: no network, no wall
    clock. Refuses (with a hint) any shape/one-way not enabled in Task 6."""
    registry = registry or DestinationRegistry()

    disabled = [s for s in spec.shapes if s != "direct"]
    if disabled:
        raise PlannerRefusal(
            f"shape(s) {', '.join(disabled)} not yet enabled",
            'shape not yet enabled (Task 10); use "shapes":["direct"] for now',
        )
    if not spec.is_round_trip:
        raise PlannerRefusal(
            "one-way search is not yet enabled",
            'add a nights range for a round-trip, e.g. "nights":"5-8" '
            "(one-way search arrives in Task 7)",
        )

    depart = spec.depart_spec
    lo, hi = spec.nights_range
    # TT must cover outbound window AND the latest possible return (out_to + max
    # nights). Rows come back un-clipped (Task 4 note) — execute() clips them.
    tt_from = depart.out_from
    tt_to = (date.fromisoformat(depart.out_to) + timedelta(days=hi)).isoformat()

    matched = _matched_destinations(spec, registry)
    want_ryanair = "ryanair" in spec.carriers
    want_wizz = "wizzair" in spec.carriers

    calls: List[CallDescriptor] = []
    for origin in sorted({o.upper() for o in spec.origins}):
        if want_ryanair:
            # RT-ANYWHERE: every Ryanair destination in ONE call (exact fares).
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
        if want_wizz:
            # TT per where-matched destination. The registry can't know which
            # routes Wizz actually serves (no public route endpoint — Task 5),
            # so we plan a TT for every matched destination as an honest upper
            # bound; execute() tolerates no_service on the ones Wizz doesn't fly.
            for dest in matched:
                calls.append(CallDescriptor(
                    provider="wizzair", endpoint="timetable", mode="timetable", shape="S2",
                    params={
                        "origin": origin,
                        "destination": dest,
                        "date_from": tt_from,
                        "date_to": tt_to,
                    },
                ))

    calls.sort(key=lambda c: c.sort_key())
    n = len(calls)
    return CallPlan(calls=calls, estimated_calls=n, estimated_seconds=round(n / rate, 1))


def check_max_calls(plan: CallPlan, max_calls: int) -> None:
    """Refuse a plan whose call count exceeds ``max_calls`` (default 40), with a
    hint on how to narrow. Called by ``run`` (not ``plan`` — inspection is free)."""
    if plan.estimated_calls > max_calls:
        raise PlannerRefusal(
            f"plan needs {plan.estimated_calls} calls, over the --max-calls {max_calls} cap",
            f"narrow the search (tighter --where, fewer origins) or raise the cap: "
            f"--max-calls {plan.estimated_calls}",
        )


# --------------------------------------------------------------------------- #
# execute                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class _Candidate:
    """One best round-trip per (origin, destination), pre-render. Exact from a
    Ryanair FarePair; approximate from a paired Wizz timetable estimate."""
    origin: str
    destination: str
    out_date: str
    return_date: str
    nights: int
    price_eur: float
    price_confidence: str
    carriers: List[str]
    legs: List[Dict[str, Any]]

    def rank_key(self) -> Tuple:
        # cheapest wins; on a price tie an exact fare beats an approximate one;
        # then carriers string for a fully deterministic order.
        approx = self.price_confidence != "exact"
        return (self.price_eur, approx, "+".join(self.carriers))


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


def _pair_timetable(
    origin: str, dest: str, out_fares: List[DayFare], ret_fares: List[DayFare],
    depart, nights_range: Tuple[int, int],
) -> Optional[_Candidate]:
    """Window-clip the (un-clipped) TT rows and pair the cheapest in-window
    outbound with the cheapest in-window inbound whose gap is within the nights
    range -> one approximate round-trip estimate for this destination."""
    lo, hi = nights_range
    o_from = date.fromisoformat(depart.out_from)
    o_to = date.fromisoformat(depart.out_to)
    outs = [f for f in out_fares if o_from <= date.fromisoformat(f.date) <= o_to]
    r_lo, r_hi = o_from + timedelta(days=lo), o_to + timedelta(days=hi)
    rets = [f for f in ret_fares if r_lo <= date.fromisoformat(f.date) <= r_hi]

    best: Optional[Tuple[float, DayFare, DayFare, int]] = None
    for o in outs:
        od = date.fromisoformat(o.date)
        for r in rets:
            n = (date.fromisoformat(r.date) - od).days
            if lo <= n <= hi:
                total = round(o.price_eur + r.price_eur, 2)
                if best is None or total < best[0]:
                    best = (total, o, r, n)
    if best is None:
        return None
    total, o, r, n = best
    return _Candidate(
        origin=origin,
        destination=dest,
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

    def _run_timetable(self, call: CallDescriptor, spec, fresh: bool):
        p = call.params
        try:
            out_fares, ret_fares = self.wizz.timetable(
                p["origin"], p["destination"], p["date_from"], p["date_to"], use_cache=not fresh,
            )
            cand = _pair_timetable(
                p["origin"], p["destination"], out_fares, ret_fares,
                spec.depart_spec, spec.nights_range,
            )
            return ("wizzair", cand, {"provider": "wizzair", "status": "ok"})
        except Exception as e:
            logger.warning("planner: wizz TT %s->%s failed: %s", p.get("origin"), p.get("destination"), e)
            return ("wizzair", None, {"provider": "wizzair", "status": status_for_exception(e), "detail": str(e)})

    def execute(self, plan: CallPlan, spec, *, fresh: bool = False) -> Dict[str, Any]:
        """Run the plan concurrently on the shared pool. Returns a dict with
        ``results`` (rendered Deal dicts), ``sources`` (frozen map),
        ``route_status`` (None when non-empty), ``exit_code`` and
        ``candidate_count`` (pre-budget, for the empty-state distinction)."""
        matched = set(_matched_destinations(spec, self.registry))
        executor = http.get_executor(self.config.max_workers)

        futures = []
        for call in plan.calls:
            if call.mode == "anywhere":
                futures.append(executor.submit(self._run_anywhere, call, fresh))
            elif call.mode == "timetable":
                futures.append(executor.submit(self._run_timetable, call, spec, fresh))

        # best candidate per (origin, destination)
        best: Dict[Tuple[str, str], _Candidate] = {}
        events: List[Dict[str, Any]] = []

        def _offer(cand: Optional[_Candidate]):
            if cand is None:
                return
            key = (cand.origin, cand.destination)
            cur = best.get(key)
            if cur is None or cand.rank_key() < cur.rank_key():
                best[key] = cand

        for fut in as_completed(futures):
            kind, payload, event = fut.result()
            events.append(event)
            if kind == "ryanair":
                for fp in payload:  # FarePairs, already duration-filtered
                    if fp.destination in matched:
                        _offer(_farepair_to_candidate(fp))
            elif kind == "wizzair":
                _offer(payload)

        aggregated = aggregate_status(events)
        provider_failed = any(not v["ok"] for v in aggregated.values())

        candidates = list(best.values())
        candidate_count = len(candidates)

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
            "sources": output.project_sources(aggregated),
            "route_status": route_status,
            "exit_code": exit_code,
            "candidate_count": candidate_count,
        }

    @staticmethod
    def _render(c: _Candidate) -> Dict[str, Any]:
        return output.build_deal(
            shape="S2",
            origin=c.origin,
            destination=c.destination,
            out_date=c.out_date,
            return_date=c.return_date,
            price_eur=c.price_eur,
            price_confidence=c.price_confidence,
            carriers=c.carriers,
            legs=c.legs,
            why=output.why_string(c.price_eur, c.price_confidence, round_trip=True),
        )

    # -- one-shot: compile + guard + execute + build envelope --------------- #
    def run(self, spec, *, max_calls: int = DEFAULT_MAX_CALLS, fresh: bool = False) -> Tuple[Dict[str, Any], int]:
        plan = self.compile(spec)
        check_max_calls(plan, max_calls)
        outcome = self.execute(plan, spec, fresh=fresh)
        env = output.envelope(
            results=outcome["results"],
            summary=output.build_summary(outcome["results"], spec.origins, outcome["route_status"]),
            sources=outcome["sources"],
            next=output.build_next(spec, outcome["results"], outcome["route_status"]),
            route_status=outcome["route_status"],
        )
        return env, outcome["exit_code"]
