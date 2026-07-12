"""Gem onward-extension (Task 15 / SEARCH-DESIGN §2b).

A gem is a curated non-airport place (small island etc.) reached via a gateway
airport + an onward ferry/bus/train chain. This module turns already-rendered
gateway Deal dicts into ADDITIONAL gem-extended variants — the plain gateway
deal always stays; the gem variant is a terminal extension, never a new shape.

Arithmetic by shape (settled design ruling):
- S2 / S3 round-trips  -> onward cost & minutes ×2 (out AND back through the gateway)
- S1 one-way           -> ×1
- S4 open-jaw          -> NOT extended in v1 (return-routing ambiguity — the two
  cities are already a two-airport product; which one does the onward hang off?)

Multi-gateway gems: a deal from EACH present gateway yields a variant; the
cheapest total per (gem, origin) survives (dedupe). Season gating is applied by
the caller via ``gems_in_play``/``forced``; here ``forced`` (an explicit ``--to``)
extends through every gateway regardless of season and marks the onward marginal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flight_deals import output
from flight_deals.models import Gem, GemGateway
from flight_deals.registry.destinations import gem_gateways_in_window

# Only point-to-point shapes carry a single gateway airport the onward chain can
# hang off. S4 (open-jaw) is deliberately excluded (documented scope cut); S5 is
# not enabled at all.
EXTENDABLE_SHAPES = {"S1", "S2", "S3"}


def build_onward(gem: Gem, gw: GemGateway, *, round_trip: bool) -> Dict[str, Any]:
    """The additive ``onward`` envelope object for one gem+gateway. ``legs`` are
    the ONE-WAY chain (reusing ``output.ground_leg`` dicts); ``cost_eur`` and
    ``minutes`` are the shape-adjusted totals (×2 for a round-trip). ``has_ferry``
    is set (and only set) when a leg crosses water, so the why-string leads that
    hop with ⛴."""
    mult = 2 if round_trip else 1
    legs = [
        output.ground_leg(l.from_place, l.to_place, l.mode, l.minutes, cost_eur=l.cost_eur)
        for l in gw.legs
    ]
    onward: Dict[str, Any] = {
        "gem": gem.slug,
        "name": gem.name,
        "legs": legs,
        "cost_eur": round(mult * gw.total_cost_eur, 2),
        "minutes": mult * gw.total_minutes,
        "note": gw.note,
        "round_trip": round_trip,
    }
    season = gw.season or gem.season
    if season:
        onward["season"] = season
    if any(l.mode == "ferry" for l in gw.legs):
        onward["has_ferry"] = True
    if gem.marginal:
        onward["marginal"] = True
    return onward


def _gem_variant(deal: Dict[str, Any], gem: Gem, gw: GemGateway) -> Dict[str, Any]:
    """One gem-extended variant of a plain gateway Deal dict. Copies the deal,
    swaps in the extended total + distinct (gem) deal_id, and attaches the
    additive ``onward`` / ``destination_display`` fields. ``why`` is set to a
    sensible interim (combine.enrich finalises it, treating onward like a
    composite so no fabricated percentile is computed against the wrong route)."""
    round_trip = deal.get("return_date") is not None
    onward = build_onward(gem, gw, round_trip=round_trip)
    total = round(float(deal["price_eur"]) + onward["cost_eur"], 2)

    v = dict(deal)
    v["price_eur"] = total
    v["onward"] = onward
    v["destination_display"] = f"{gem.name} (via {gw.airport})"
    v["deal_id"] = output.deal_id(
        deal["origin"], deal["destination"], deal["out_date"],
        deal.get("return_date"), deal["shape"], deal["carriers"], gem_slug=gem.slug,
    )
    if "estimated_price_eur" in v:
        v["estimated_price_eur"] = round(float(deal["estimated_price_eur"]) + onward["cost_eur"], 2)
    v["why"] = output.why_string(total, deal["price_confidence"], round_trip=round_trip) \
        + output.onward_why_suffix(v)
    return v


def extend_deals(
    deals: List[Dict[str, Any]],
    gems: List[Gem],
    *,
    window: Optional[tuple] = None,
    forced: bool = False,
) -> List[Dict[str, Any]]:
    """Produce gem-extended variants of ``deals`` (the plain deals are left
    untouched — the caller decides whether to keep them). For each gem, for each
    in-window gateway (all gateways when ``forced``), extend every extendable
    gateway deal; keep the cheapest total per (gem, origin).

    Returns a flat list of new variant dicts (possibly empty)."""
    variants: List[Dict[str, Any]] = []
    for gem in gems:
        gateways = gem.gateways if forced else gem_gateways_in_window(gem, window)
        by_airport = {gw.airport: gw for gw in gateways}
        best: Dict[tuple, Dict[str, Any]] = {}
        for deal in deals:
            if deal.get("shape") not in EXTENDABLE_SHAPES:
                continue
            if deal.get("onward"):  # never extend an already-extended deal
                continue
            gw = by_airport.get(deal["destination"])
            if gw is None:
                continue
            v = _gem_variant(deal, gem, gw)
            key = (gem.slug, deal["origin"])
            cur = best.get(key)
            if cur is None or (v["price_eur"], v["deal_id"]) < (cur["price_eur"], cur["deal_id"]):
                best[key] = v
        variants.extend(best.values())
    return variants
