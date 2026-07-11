"""Estimate→confirm (UPGRADE-PLAN §4, Global Constraint 5, Task 7 req 2).

Approximate deals come from Wizz timetable pairings priced off day-level minima
over a *window* (possibly cached). Before we display them — and before Task 8
would ever alert on them — every deal in the *confirm margin band*
(``planner.execute()``'s ``confirm_band``: a bounded superset of the final
display set that also covers deals within 20% over budget or just outside the
top-N, so a confirmed price can rescue or back-fill a slot — see
``intents.run_search``) is re-checked with a fresh, cache-bypassed timetable
query on the **exact** dates. The confirmed figure replaces ``price_eur``; the
original windowed estimate is retained in ``estimated_price_eur`` so a
consumer can see the movement.

Ryanair FarePairs are already exact (RT-EXACT confidence), so they are skipped.
A Wizz deal that cannot be re-confirmed stays ``approximate`` and untouched —
never silently promoted — because Task 8's alert machine only fires on exact
(or confirmed) prices.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _confirm_wizz(deal: Dict[str, Any], wizz) -> None:
    """Re-query Wizz on the exact out/return dates (cache-bypassed) and refine
    ``price_eur`` in place, retaining the windowed estimate. One-way deals
    confirm the outbound only."""
    origin, dest = deal["origin"], deal["destination"]
    out_date = deal["out_date"]
    return_date = deal.get("return_date")
    lo = out_date
    hi = return_date or out_date
    out_fares, ret_fares = wizz.timetable(origin, dest, lo, hi, use_cache=False)

    out_hit = min((f for f in out_fares if f.date == out_date), key=lambda f: f.price_eur, default=None)
    if out_hit is None:
        return  # unconfirmable — leave the estimate as-is, stays approximate
    if return_date is None:
        confirmed = round(out_hit.price_eur, 2)
    else:
        ret_hit = min((f for f in ret_fares if f.date == return_date), key=lambda f: f.price_eur, default=None)
        if ret_hit is None:
            return
        confirmed = round(out_hit.price_eur + ret_hit.price_eur, 2)

    estimate = deal["price_eur"]
    deal["estimated_price_eur"] = estimate
    deal["price_eur"] = confirmed
    # Reflect the confirmed figure on the legs too, so leg prices stay coherent.
    for leg, hit in zip(deal.get("legs", []), (out_hit, None if return_date is None else ret_hit)):
        if hit is not None and leg.get("type") == "flight":
            leg["price_eur"] = round(hit.price_eur, 2)


def _confirm_openjaw(deal: Dict[str, Any], ryanair) -> None:
    """Re-confirm an S4 open-jaw deal's two one-way Ryanair legs on their EXACT
    dates (cache-bypassed) via ``oneway_fares``, refining ``price_eur`` in place.
    Both legs are exact per-leg (Ryanair CAL/OW), so the deal stays ``exact``;
    this locks the month-level estimate to the actual per-date fares (and is the
    honest "two separate one-way tickets" confirmation). The D1<->D2 ground cost
    is preserved from the deal's ground summary. Unconfirmable -> estimate kept."""
    flight_legs = [l for l in deal.get("legs", []) if l.get("type") == "flight"]
    if len(flight_legs) != 2:
        return
    out_leg, ret_leg = flight_legs[0], flight_legs[1]
    out_hit = _exact_oneway(ryanair, out_leg["origin"], out_leg["destination"], deal["out_date"])
    if out_hit is None:
        return
    ret_hit = _exact_oneway(ryanair, ret_leg["origin"], ret_leg["destination"], deal["return_date"])
    if ret_hit is None:
        return
    ground_cost = float((deal.get("ground") or {}).get("cost_eur") or 0.0)
    confirmed = round(out_hit.price_eur + ret_hit.price_eur + ground_cost, 2)
    if confirmed == deal["price_eur"]:
        return
    deal["estimated_price_eur"] = deal["price_eur"]
    deal["price_eur"] = confirmed
    out_leg["price_eur"] = round(out_hit.price_eur, 2)
    ret_leg["price_eur"] = round(ret_hit.price_eur, 2)


def _exact_oneway(ryanair, origin: str, dest: str, day: str):
    """Cheapest Ryanair one-way fare for ``origin->dest`` on the exact ``day``
    (cache-bypassed). ``None`` if nothing bookable that day."""
    fares = ryanair.oneway_fares(origin, dest, out_from=day, out_to=day, use_cache=False)
    return min((f for f in fares if f.date == day), key=lambda f: f.price_eur, default=None)


def confirm(deals: List[Dict[str, Any]], *, wizz, ryanair=None) -> None:
    """Confirm each deal in the display set in place before display/alert
    (Global Constraint 5). Approximate (Wizz) deals are re-priced on their exact
    dates; S4 open-jaw deals re-confirm both one-way legs exact (Ryanair). Exact
    direct/extended-origin Ryanair deals (S2/S3) are already exact and skipped.
    Any per-deal failure is logged and the estimate kept — never fatal."""
    for deal in deals:
        try:
            if deal.get("shape") == "S4" and ryanair is not None:
                _confirm_openjaw(deal, ryanair)
                continue
            if deal.get("price_confidence") == "exact":
                continue
            if "wizzair" not in deal.get("carriers", []):
                continue
            _confirm_wizz(deal, wizz)
        except Exception as e:  # noqa: BLE001 — logged, estimate retained
            logger.warning(
                "confirm: could not confirm %s %s->%s: %s",
                deal.get("deal_id"), deal.get("origin"), deal.get("destination"), e,
            )
