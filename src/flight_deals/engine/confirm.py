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


def confirm(deals: List[Dict[str, Any]], *, wizz) -> None:
    """Confirm every approximate (Wizz) deal in the display set in place. Exact
    deals are skipped. Any per-deal failure is logged and the estimate kept —
    confirmation is best-effort, never fatal to the response."""
    for deal in deals:
        if deal.get("price_confidence") == "exact":
            continue
        if "wizzair" not in deal.get("carriers", []):
            continue
        try:
            _confirm_wizz(deal, wizz)
        except Exception as e:  # noqa: BLE001 — logged, estimate retained
            logger.warning(
                "confirm: could not confirm %s %s->%s: %s",
                deal.get("deal_id"), deal.get("origin"), deal.get("destination"), e,
            )
