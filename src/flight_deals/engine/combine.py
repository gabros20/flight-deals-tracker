"""Deal enrichment: history-backed ``why`` strings + standout/solid/baseline
grouping (SEARCH-DESIGN §2, Task 7 req 4).

This is where a rendered Deal dict gains its honest, falsifiable narration.
A deal is **standout** only when it is ≥25% below the route's typical
(median) price AND the route has ≥5 observations; **solid** when merely below
typical; **baseline** otherwise. Below the 5-observation floor we refuse to
compute a percentile and say "insufficient history" instead of fabricating one
(Global Constraint 3).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flight_deals import output

STANDOUT_THRESHOLD = 0.25  # ≥25% below typical
MIN_OBS_FOR_STANDOUT = 5


def _why(deal: Dict[str, Any], cmp: Dict[str, Any]) -> str:
    """One falsifiable sentence. With enough history: price vs typical + %below
    + observation count. Without it: the factual, non-comparative fallback plus
    an honest "insufficient history" label."""
    price = deal["price_eur"]
    approx = deal["price_confidence"] != "exact"
    prefix = "~" if approx else ""
    if cmp.get("sufficient") and cmp.get("median"):
        median = cmp["median"]
        pct = cmp.get("pct_vs_typical") or 0.0
        if pct >= 0:
            rel = f"{int(round(pct * 100))}% below"
        else:
            rel = f"{int(round(-pct * 100))}% above"
        return (
            f"{prefix}€{price:.0f} vs typical €{median:.0f} for this route, "
            f"{rel}, {cmp['count']} observations"
        )
    base = output.why_string(price, deal["price_confidence"], round_trip=deal.get("return_date") is not None)
    n = cmp.get("count", 0)
    return f"{base} (insufficient history: {n} observation{'s' if n != 1 else ''})"


def _group(deal: Dict[str, Any], cmp: Dict[str, Any]) -> str:
    if not cmp.get("sufficient") or not cmp.get("median"):
        return "baseline"
    price = deal["price_eur"]
    median = cmp["median"]
    if price <= (1.0 - STANDOUT_THRESHOLD) * median:
        return "standout"
    if price < median:
        return "solid"
    return "baseline"


def enrich(deals: List[Dict[str, Any]], history_store, *, window_days: Optional[int] = None) -> None:
    """Mutate each rendered Deal dict in place: rewrite ``why`` from history and
    attach a ``group`` (standout/solid/baseline). Safe to call with an empty
    list. A history-store failure degrades a single deal to its factual
    fallback rather than aborting the whole response."""
    for d in deals:
        try:
            cmp = history_store.compare(d["origin"], d["destination"], d["price_eur"], window_days=window_days)
        except Exception:
            cmp = {"count": 0, "median": None, "sufficient": False, "pct_vs_typical": None}
        # History rewrites the base sentence; re-append the honest ground clause
        # (S3/S4) so a shaped deal never loses its "incl. bus"/"open-jaw" detail.
        d["why"] = _why(d, cmp) + output.ground_why_suffix(d)
        d["group"] = _group(d, cmp)
