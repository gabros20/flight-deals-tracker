"""S5 self-transfer via a hub (Task 16 / SEARCH-DESIGN §2, S5).

Two SEPARATE same-day Ryanair tickets, O→H then H→D (and the mirror on the
return), booked as two bookings through a hub H. A missed connection is the
traveller's own risk, so this shape is held to the strictest honesty bar in the
project:

* **Same-airport connections only** (no BGY→MXP metro hop) — which is exactly
  what lets the connect math run on the farfnd airport-LOCAL naive datetimes:
  ``arrival_at`` of the inbound leg and ``departure_at`` of the outbound leg are
  both in hub H's local zone, so their delta is timezone-correct without any tz
  database. Overnight arrivals (``arrival_at`` on the next calendar day, e.g.
  21:40→00:05) are handled because the delta is computed on full datetimes,
  never on date arithmetic.
* **MCT gate**: a connection must be at least ``min_connect_minutes`` (default
  180 — a 3h missed-connection floor) and at most ``max_connect_minutes``
  (default 480 — beyond that it's a stopover, not a transfer). Both bounds drop.
* **Two-stage funnel**: this module owns the PURE half — discovery composition
  (same-day MCT-plausible outbound pairs straight from the anywhere sweep data)
  and shortlist ranking. The provider-call orchestration (discovery fan-out and
  the 4-leg exact-date verification) lives in ``engine/planner.py`` so it runs
  through the shared executor + token bucket and is counted in the plan
  estimate. A candidate is a *deal* ONLY after all four legs verify exact and
  both connections pass MCT — unverified candidates are NEVER displayed or
  alerted (hard rule).

Nothing here touches the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from flight_deals.models import DayFare

logger = logging.getLogger(__name__)

# Shortlist size: the top-N discovery candidates (cheapest outbound composite)
# that earn a return-window sweep + exact-date verification. Bounds the
# verification fan-out regardless of how many hubs/candidates discovery composed.
VIA_HUB_SHORTLIST = 6

# Return-window sweep budget (Task 17). Per shortlisted candidate the outbound is
# fixed from discovery (already carries times), and only the RETURN side is swept:
#   * ``CAL_CALLS_PER_MONTH`` cheapestPerDay calls per return month (D→H and H→O,
#     day-level minima, 6h calendar cache tier, deduped by (o,d,month) per run);
#   * the best-priced valid return date is time-verified with 2 fresh exact-date
#     oneWayFares calls, and on an MCT/bookability failure ONE retry runs on the
#     next-best-priced date (2 more) — so ``RETURN_EXACT_CALLS_PER_CANDIDATE`` = 4.
# The return month span is capped at ``RETURN_MONTHS_CAP`` months.
RETURN_MONTHS_CAP = 2
CAL_CALLS_PER_MONTH = 2
RETURN_EXACT_CALLS_PER_CANDIDATE = 4


def reserve_verify_calls(n_return_months: int) -> int:
    """The honest per-candidate verification ceiling for the return-window sweep:
    ``CAL_CALLS_PER_MONTH × n_return_months`` day-level calls (worst case, before
    the (o,d,month) dedupe that can only *reduce* it) plus the 4 exact-date calls
    (2 primary + 2 retry). Reserved × the shortlist in ``estimated_calls`` so
    ``--max-calls`` never blows mid-run."""
    months = max(1, min(n_return_months, RETURN_MONTHS_CAP))
    return CAL_CALLS_PER_MONTH * months + RETURN_EXACT_CALLS_PER_CANDIDATE


def parse_dt(iso: Optional[str]) -> Optional[datetime]:
    """Parse a farfnd airport-local naive ISO datetime, or ``None``."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def connect_minutes(arrival_at: Optional[str], departure_at: Optional[str]) -> Optional[int]:
    """Whole minutes between arriving at the hub (``arrival_at`` of the inbound
    leg) and departing it (``departure_at`` of the outbound leg). ``None`` when
    either instant is missing/unparseable. May be **negative** if the outbound
    departs before the inbound lands (an impossible connection) — the caller's
    :func:`mct_ok` rejects that. Computed on full datetimes so an overnight
    arrival (next-day ``arrival_at``) is measured correctly, never by date
    subtraction."""
    a, d = parse_dt(arrival_at), parse_dt(departure_at)
    if a is None or d is None:
        return None
    return int((d - a).total_seconds() // 60)


def mct_ok(minutes: Optional[int], min_connect: int, max_connect: int) -> bool:
    """A connection is valid iff ``min_connect <= minutes <= max_connect``.
    Boundaries: at exactly ``min_connect`` it passes (>=); at exactly
    ``max_connect`` it passes (<=); anything below the floor (incl. negative /
    None) or above the ceiling fails."""
    if minutes is None:
        return False
    return min_connect <= minutes <= max_connect


@dataclass(frozen=True)
class DiscoveredS5:
    """One same-day, MCT-plausible outbound self-transfer composed straight from
    the discovery (anywhere-sweep) data: O→``hub``→``destination`` on
    ``out_date``, with the outbound-composite price and the discovery-level
    connection gap. This is a *candidate*, not a deal — the return leg and the
    exact fares/times are only pinned during verification.

    ``leg1``/``leg2`` are the actual outbound DayFares (O→hub, hub→dest) straight
    from the discovery sweep: exact one-way fares carrying real times, so the
    outbound is reused as-is at verification (its MCT was already checked here)
    and contributes exact prices to the final total (no estimate leakage)."""
    hub: str
    destination: str
    out_date: str
    out_price_eur: float
    connect_out_minutes: int
    leg1: Optional[DayFare] = None
    leg2: Optional[DayFare] = None


def discover(
    origin: str,
    hubs: Sequence[str],
    origin_fares: Sequence[DayFare],
    hub_fares: Dict[str, List[DayFare]],
    matched_dests: Sequence[str],
    *,
    min_connect: int,
    max_connect: int,
) -> List[DiscoveredS5]:
    """Compose same-day, MCT-plausible outbound self-transfer candidates from the
    discovery data (PURE — no network).

    * ``origin_fares``: the O→anywhere OW sweep (each carries times); filtered to
      hub destinations, cheapest kept per hub (the leg1 O→H candidate).
    * ``hub_fares``: ``{hub: H→anywhere OW sweep}``; each H→D filtered to the
      where-matched destination set (and never back to the origin or another
      hub).
    * A pair composes iff leg1 and leg2 are on the **same calendar day** and
      their same-airport connection gap passes :func:`mct_ok`.

    One candidate per (hub, destination); when two hubs reach the same
    destination both are emitted (the executor's dedup keeps the cheapest deal).
    """
    origin = origin.upper()
    hubs_set = {h.upper() for h in hubs}
    matched = {d.upper() for d in matched_dests}

    # Cheapest O→H per hub (the leg1 candidate); needs a real arrival instant.
    leg1_by_hub: Dict[str, DayFare] = {}
    for f in origin_fares:
        h = f.destination.upper()
        if h not in hubs_set or not f.arrival_at:
            continue
        cur = leg1_by_hub.get(h)
        if cur is None or f.price_eur < cur.price_eur:
            leg1_by_hub[h] = f

    out: List[DiscoveredS5] = []
    for hub, leg2_list in hub_fares.items():
        hub = hub.upper()
        leg1 = leg1_by_hub.get(hub)
        if leg1 is None:
            continue
        for leg2 in leg2_list:
            d = leg2.destination.upper()
            if d not in matched or d == origin or d in hubs_set:
                continue
            if leg2.date != leg1.date:  # same-day self-transfer only
                continue
            if not leg2.departure_at:
                continue
            cm = connect_minutes(leg1.arrival_at, leg2.departure_at)
            if not mct_ok(cm, min_connect, max_connect):
                continue
            out.append(DiscoveredS5(
                hub=hub, destination=d, out_date=leg1.date,
                out_price_eur=round(leg1.price_eur + leg2.price_eur, 2),
                connect_out_minutes=cm, leg1=leg1, leg2=leg2,
            ))
    return out


def shortlist(
    candidates: Sequence[DiscoveredS5], size: int = VIA_HUB_SHORTLIST
) -> Tuple[List[DiscoveredS5], int]:
    """The cheapest ``size`` discovery candidates (by outbound composite price,
    then hub/destination for a deterministic order) plus the count DROPPED — no
    silent cap: the caller logs the drop so the estimate stays honest."""
    ordered = sorted(candidates, key=lambda c: (c.out_price_eur, c.hub, c.destination, c.out_date))
    kept = ordered[:size]
    return kept, max(0, len(ordered) - len(kept))


@dataclass(frozen=True)
class ReturnDateOption:
    """One candidate return date for a shortlisted self-transfer, priced on the
    CAL day-level minima: fly ``dest→hub→origin`` on ``ret_date``. ``ret_price_eur``
    is the leg3+leg4 selection minimum — a *selection* price, replaced by the
    exact fares once ``ret_date`` is time-verified (no estimate leakage)."""
    ret_date: str
    leg3_price_eur: float  # dest -> hub
    leg4_price_eur: float  # hub -> origin
    ret_price_eur: float   # leg3 + leg4 (CAL-selection minima)


def _cheapest_by_date(fares: Sequence[DayFare]) -> Dict[str, DayFare]:
    """Cheapest fare per calendar date (CAL rows are already day-level minima, but
    a multi-month concat can carry duplicates; keep the cheapest defensively)."""
    best: Dict[str, DayFare] = {}
    for f in fares:
        cur = best.get(f.date)
        if cur is None or f.price_eur < cur.price_eur:
            best[f.date] = f
    return best


def select_return_dates(
    out_date: str,
    dest_hub_fares: Sequence[DayFare],
    hub_origin_fares: Sequence[DayFare],
    *,
    nights_lo: int,
    nights_hi: int,
) -> List[ReturnDateOption]:
    """Rank candidate return dates for a self-transfer (PURE — no network).

    A date qualifies iff it lies inside the nights window
    ``[out_date + nights_lo, out_date + nights_hi]`` AND **both** return legs
    exist in the CAL day-level data on that date (``dest→hub`` and ``hub→origin``).
    Options are ranked by the leg3+leg4 selection sum (then date for a stable
    order); the caller time-verifies them cheapest-first, retrying the next-best
    once if the cheapest fails MCT/bookability.

    This is the yield fix over Task 16's single fixed return date (out+min-nights):
    it finds the cheapest return date where the two independently-cheapest legs
    actually both fly, giving verification a real chance to connect."""
    out = date.fromisoformat(out_date)
    lo_d = out + timedelta(days=nights_lo)
    hi_d = out + timedelta(days=nights_hi)
    leg3_by_date = _cheapest_by_date(dest_hub_fares)
    leg4_by_date = _cheapest_by_date(hub_origin_fares)
    options: List[ReturnDateOption] = []
    for day, f3 in leg3_by_date.items():
        dd = date.fromisoformat(day)
        if not (lo_d <= dd <= hi_d):
            continue
        f4 = leg4_by_date.get(day)
        if f4 is None:  # both legs must exist on the same return date
            continue
        options.append(ReturnDateOption(
            ret_date=day, leg3_price_eur=f3.price_eur, leg4_price_eur=f4.price_eur,
            ret_price_eur=round(f3.price_eur + f4.price_eur, 2),
        ))
    options.sort(key=lambda o: (o.ret_price_eur, o.ret_date))
    return options
