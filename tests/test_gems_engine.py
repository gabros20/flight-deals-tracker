"""Task 15b — gem onward-extension ENGINE (registry matching, planner/intents
extension, envelope, deal_id, watch thresholds).

Fixtures-only (Global Constraint 10): providers are monkeypatched to echo a
crafted Ryanair round-trip to a gem gateway; no network. Gem data comes from the
committed ``data/destinations.json`` (Task 15a), read through the real registry.
"""

from datetime import date, datetime, timezone

import pytest

from flight_deals.engine import gems as gems_engine
from flight_deals.engine.intents import run_search
from flight_deals.engine.planner import Planner
from flight_deals.models import FareLeg, FarePair
from flight_deals.output import build_deal, deal_id, why_string
from flight_deals.registry.destinations import (
    DestinationRegistry,
    gem_gateways_in_window,
    season_months,
    season_overlaps_window,
)

FIXED_TODAY = date(2026, 1, 1)
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakeHistory:
    def compare(self, origin, destination, price, window_days=None):
        return {"count": 0, "median": None, "min": None,
                "pct_vs_typical": None, "sufficient": False, "best_this_month": False}


def _collector():
    seen = []
    return seen, (lambda d, now=None: seen.append(d["deal_id"]))


def _fp(origin, dest, out, ret, total):
    half = round(total / 2, 2)
    return FarePair(
        origin=origin, destination=dest, out_date=out, return_date=ret, nights=6,
        total_price_eur=total, currency_original="EUR", price_confidence="exact",
        carrier="ryanair", source_endpoint="farfnd/roundTripFares",
        outbound=FareLeg(origin=origin, destination=dest, date=out, price_eur=half, carrier="ryanair"),
        inbound=FareLeg(origin=dest, destination=origin, date=ret, price_eur=half, carrier="ryanair"),
    )


def _rho_planner(total=100.0):
    """Ryanair returns one exact BUD→RHO round-trip (RHO is Halki's gateway);
    Wizz serves nothing."""
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda origin, dest=None, **kw: [
        _fp("BUD", "RHO", "2026-08-23", "2026-08-29", total)
    ]
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


def _reg():
    return DestinationRegistry()


def _gem(reg, slug):
    return reg.resolve_gem(slug)


# --------------------------------------------------------------------------- #
# registry: resolve, typo hint, marginal exclusion, season gating             #
# --------------------------------------------------------------------------- #
def test_resolve_gem_by_slug_and_name_and_typo_hint():
    reg = _reg()
    assert reg.resolve_gem("halki").slug == "halki"
    assert reg.resolve_gem("Halki").slug == "halki"        # case-insensitive name
    assert reg.resolve_gem("nope-not-a-gem") is None
    # typo hint routes through destination_suggestion (gem slugs in the pool)
    assert reg.destination_suggestion("halky") == "halki"


def test_gems_matching_excludes_marginal_by_default():
    reg = _reg()
    m = reg.gems_matching("hidden-gem & seaside")
    slugs = {g.slug for g in m}
    assert "pserimos" not in slugs                          # marginal — hidden by default
    incl = {g.slug for g in reg.gems_matching("hidden-gem & seaside", include_marginal=True)}
    assert "pserimos" in incl


def test_gems_matching_season_gates_both_sides():
    """Stromboli is a jun-sep (gem-level season) gem; it matches a July window
    and is excluded from a January one — season gating on BOTH sides."""
    reg = _reg()
    summer = ("2026-07-10", "2026-07-14")
    winter = ("2026-01-10", "2026-01-14")
    assert "stromboli" in {g.slug for g in reg.gems_matching("hidden-gem & seaside & italy", window=summer)}
    assert "stromboli" not in {g.slug for g in reg.gems_matching("hidden-gem & seaside & italy", window=winter)}


def test_season_helpers_wrap_and_overlap():
    assert season_months("jun-sep") == {6, 7, 8, 9}
    assert season_months("nov-feb") == {11, 12, 1, 2}       # wraps the year end
    assert season_overlaps_window("jun-sep", ("2026-07-01", "2026-07-05")) is True
    assert season_overlaps_window("jun-sep", ("2026-01-01", "2026-01-05")) is False
    assert season_overlaps_window(None, ("2026-01-01", "2026-01-05")) is True  # no season = year-round


# --------------------------------------------------------------------------- #
# extension arithmetic: ×2 round-trip / ×1 one-way / S4 not extended           #
# --------------------------------------------------------------------------- #
def _plain_rho_deal(shape="S2", return_date="2026-08-29", price=100.0):
    return build_deal(
        shape=shape, origin="BUD", destination="RHO", out_date="2026-08-23",
        return_date=return_date, price_eur=price, price_confidence="exact",
        carriers=["ryanair"], legs=[{"type": "flight", "origin": "BUD", "destination": "RHO",
                                     "carrier": "ryanair", "departure_date": "2026-08-23", "price_eur": price}],
        why="x",
    )


def test_extension_arithmetic_roundtrip_doubles():
    reg = _reg()
    halki = _gem(reg, "halki")   # RHO gateway, total_cost_eur 10.0, ferry chain
    variants = gems_engine.extend_deals([_plain_rho_deal()], [halki], forced=True)
    assert len(variants) == 1
    v = variants[0]
    assert v["onward"]["cost_eur"] == 20.0        # 10.0 × 2 (out + back)
    assert v["onward"]["minutes"] == 240          # 120 × 2
    assert v["price_eur"] == 120.0                # 100 fare + 20 onward
    assert v["onward"]["round_trip"] is True


def test_extension_arithmetic_oneway_single():
    reg = _reg()
    halki = _gem(reg, "halki")
    ow = _plain_rho_deal(shape="S1", return_date=None, price=50.0)
    v = gems_engine.extend_deals([ow], [halki], forced=True)[0]
    assert v["onward"]["cost_eur"] == 10.0        # ×1 for one-way
    assert v["onward"]["minutes"] == 120
    assert v["price_eur"] == 60.0
    assert v["onward"]["round_trip"] is False


def test_s4_open_jaw_is_not_extended():
    reg = _reg()
    halki = _gem(reg, "halki")
    s4 = _plain_rho_deal(shape="S4")   # shape S4 -> excluded from extension
    assert gems_engine.extend_deals([s4], [halki], forced=True) == []


# --------------------------------------------------------------------------- #
# onward envelope shape: ground_leg dicts + ⛴️ + ~ markers                      #
# --------------------------------------------------------------------------- #
def test_onward_envelope_shape_ferry_and_tilde():
    reg = _reg()
    halki = _gem(reg, "halki")
    v = gems_engine.extend_deals([_plain_rho_deal()], [halki], forced=True)[0]
    o = v["onward"]
    assert o["gem"] == "halki" and o["name"] == "Halki"
    assert o["has_ferry"] is True
    assert o["note"]                                         # operator/source note preserved
    assert v["destination_display"] == "Halki (via RHO)"
    # legs are reused output.ground_leg dicts (type=ground, has cost/minutes)
    assert all(l["type"] == "ground" for l in o["legs"])
    assert any(l["mode"] == "ferry" for l in o["legs"])
    # why-string discloses the sea crossing (⛴️) and marks the estimate (~)
    assert "⛴️" in v["why"] and "~€" in v["why"] and "to Halki" in v["why"]


# --------------------------------------------------------------------------- #
# deal_id: additive gem component + distinctness + golden vector               #
# --------------------------------------------------------------------------- #
def test_deal_id_gem_component_distinct_and_golden():
    plain = deal_id("BUD", "RHO", "2026-08-23", "2026-08-29", "S2", ["ryanair"])
    gem = deal_id("BUD", "RHO", "2026-08-23", "2026-08-29", "S2", ["ryanair"], gem_slug="halki")
    assert plain != gem
    # existing (non-gem) id is byte-unchanged; gem id is the frozen golden vector
    assert plain == "0c2911c971"
    assert gem == "d78e104b78"


# --------------------------------------------------------------------------- #
# e2e: --to gem shows ONLY variants; --where shows BOTH                        #
# --------------------------------------------------------------------------- #
def test_to_gem_displays_only_variants():
    seen, snap = _collector()
    env, code = run_search(
        where=None, to="halki", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=_rho_planner(), registry=_reg(),
        history_store=_FakeHistory(), snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0 and env["results"]
    # only gem-extended variants — no bare RHO gateway deal
    assert all(d.get("onward") and d["onward"]["gem"] == "halki" for d in env["results"])
    assert env["results"][0]["price_eur"] == 120.0
    assert "Halki" in env["summary"]
    assert seen == [d["deal_id"] for d in env["results"]]   # each variant snapshotted


def test_where_matching_shows_both_plain_and_gem():
    env, code = run_search(
        where="quiet & hidden-gem & greece", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=_rho_planner(), registry=_reg(),
        history_store=_FakeHistory(), snapshotter=_collector()[1],
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0
    has_plain_rho = any(d["destination"] == "RHO" and not d.get("onward") for d in env["results"])
    has_halki = any(d.get("onward", {}).get("gem") == "halki" for d in env["results"])
    assert has_plain_rho and has_halki


# --------------------------------------------------------------------------- #
# budget recut applies to the EXTENDED total                                   #
# --------------------------------------------------------------------------- #
def test_budget_recut_uses_extended_total():
    # Plain RHO fare 100; Halki extended total 120. Budget 110 keeps the plain
    # gateway deal but drops the gem variant (its onward pushes it over budget).
    env, code = run_search(
        where="quiet & hidden-gem & greece", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=110, origins=["BUD"], planner=_rho_planner(), registry=_reg(),
        history_store=_FakeHistory(), snapshotter=_collector()[1],
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0
    assert any(d["destination"] == "RHO" and not d.get("onward") for d in env["results"])
    assert all(d["price_eur"] <= 110 for d in env["results"])
    assert not any(d.get("onward", {}).get("gem") == "halki" for d in env["results"])


def test_to_gem_over_budget_is_empty():
    env, code = run_search(
        where=None, to="halki", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=110, origins=["BUD"], planner=_rho_planner(), registry=_reg(),
        history_store=_FakeHistory(), snapshotter=_collector()[1],
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    # only-variants + the sole variant (120) is over the 110 budget -> empty
    assert code == 0 and env["results"] == []
    assert env["route_status"] == "no_match"


# --------------------------------------------------------------------------- #
# marginal gem via --to surfaces its caveat note prominently                   #
# --------------------------------------------------------------------------- #
def test_marginal_gem_via_to_surfaces_caveat():
    """Pserimos is marginal (gateway KGS). Reached via --to, its variant carries
    the day-trip/connection caveat in the why-string."""
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda origin, dest=None, **kw: [
        _fp("BUD", "KGS", "2026-08-23", "2026-08-29", 100.0)
    ]
    planner.wizz.timetable = lambda *a, **k: ([], [])
    env, code = run_search(
        where=None, to="pserimos", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=planner, registry=_reg(),
        history_store=_FakeHistory(), snapshotter=_collector()[1],
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0 and env["results"]
    v = env["results"][0]
    assert v["onward"]["gem"] == "pserimos" and v["onward"]["marginal"] is True
    assert "marginal:" in v["why"]


# --------------------------------------------------------------------------- #
# watch/alert threshold applies to the EXTENDED total (no alert-machine change)#
# --------------------------------------------------------------------------- #
def test_watch_threshold_fires_on_extended_total(tmp_path):
    from flight_deals.state.alert_state import AlertMachine

    reg = _reg()
    halki = _gem(reg, "halki")
    gem_deal = gems_engine.extend_deals([_plain_rho_deal()], [halki], forced=True)[0]
    assert gem_deal["price_eur"] == 120.0 and gem_deal["price_confidence"] == "exact"

    # A watch capped at €110 must NOT fire (the extended total is €120)...
    m = AlertMachine(path=tmp_path / "a.json")
    assert m.evaluate(search_name="halki-watch", deal=gem_deal, max_price=110.0, now=FIXED_NOW) is False
    # ...but a €130 cap fires (threshold is on the fare+onward total, S3/S4 precedent).
    m2 = AlertMachine(path=tmp_path / "b.json")
    assert m2.evaluate(search_name="halki-watch", deal=gem_deal, max_price=130.0, now=FIXED_NOW) is True


# --------------------------------------------------------------------------- #
# multi-gateway dedupe: cheapest total per (gem, origin)                       #
# --------------------------------------------------------------------------- #
def test_multi_gateway_dedupe_keeps_cheapest():
    """Milos has ATH (cheap) and JTR (pricey) gateways. Given a deal to each,
    extension keeps ONE Milos variant — the cheaper total."""
    reg = _reg()
    milos = _gem(reg, "milos")
    ath = _plain_rho_deal()          # reuse builder; override destination below
    ath = dict(ath); ath["destination"] = "ATH"; ath["price_eur"] = 100.0
    jtr = dict(ath); jtr["destination"] = "JTR"; jtr["price_eur"] = 100.0
    variants = gems_engine.extend_deals([ath, jtr], [milos], forced=True)
    assert len(variants) == 1                      # one Milos, not two
    # ATH onward (cost 40 ×2=80) is cheaper than JTR (74 ×2=148) -> ATH wins
    assert variants[0]["destination"] == "ATH"
