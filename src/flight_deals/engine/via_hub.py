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
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from flight_deals.models import DayFare

logger = logging.getLogger(__name__)

# Shortlist size: the top-N discovery candidates (cheapest outbound composite)
# that earn a full 4-leg exact-date verification. Bounds verification fan-out at
# N×4 calls regardless of how many hubs/candidates discovery composed.
VIA_HUB_SHORTLIST = 6
VERIFY_LEGS_PER_CANDIDATE = 4


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
    exact fares/times are only pinned during verification."""
    hub: str
    destination: str
    out_date: str
    out_price_eur: float
    connect_out_minutes: int


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
                connect_out_minutes=cm,
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
