"""Unit tests for engine/confirm.py's estimate->confirm pass — in particular
the FAILS invariant: when the exact re-query finds no matching hit, the deal
keeps its windowed estimate untouched and nothing is fabricated. This
invariant is load-bearing for Task 8's alert gate, which only ever fires on
exact/confirmed prices — a silently-promoted estimate would be a false alert.
"""

from flight_deals.engine import confirm as confirm_mod
from flight_deals.models import DayFare


def _wizz_deal(price=90.0):
    return {
        "deal_id": "abc1234567", "shape": "S2", "origin": "BUD", "destination": "CFU",
        "out_date": "2026-08-23", "return_date": "2026-08-29",
        "price_eur": price, "price_confidence": "approximate", "carriers": ["wizzair"],
        "legs": [
            {"type": "flight", "origin": "BUD", "destination": "CFU", "carrier": "wizzair",
             "date": "2026-08-23", "price_eur": price / 2},
            {"type": "flight", "origin": "CFU", "destination": "BUD", "carrier": "wizzair",
             "date": "2026-08-29", "price_eur": price / 2},
        ],
    }


def _fare(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="approximate",
                   carrier="wizzair", source_endpoint="wizz/timetable")


class _NoHitWizz:
    """Exact re-query comes back with fares, but none on the exact dates we
    need -> no matching hit for either leg."""

    def timetable(self, origin, dest, date_from, date_to, use_cache=True):
        return (
            [_fare(origin, dest, "2026-09-01", 10.0)],   # wrong out_date
            [_fare(dest, origin, "2026-09-08", 10.0)],   # wrong return_date
        )


class _RaisingWizz:
    """The re-query itself blows up (network, parse error, ...)."""

    def timetable(self, origin, dest, date_from, date_to, use_cache=True):
        raise RuntimeError("boom")


def test_confirm_fails_keeps_estimate_untouched_and_fabricates_nothing():
    deal = _wizz_deal(price=90.0)
    confirm_mod.confirm([deal], wizz=_NoHitWizz())

    assert deal["price_eur"] == 90.0                   # kept the estimate
    assert deal["price_confidence"] == "approximate"   # never silently promoted
    assert "estimated_price_eur" not in deal           # nothing fabricated
    assert deal["legs"][0]["price_eur"] == 45.0        # legs untouched too
    assert deal["legs"][1]["price_eur"] == 45.0


def test_confirm_one_leg_matches_other_does_not_still_fails_whole_deal():
    """A partial hit (outbound confirmable, inbound not) must not half-confirm
    a round-trip price — the deal stays fully on its estimate."""

    class _HalfHitWizz:
        def timetable(self, origin, dest, date_from, date_to, use_cache=True):
            return (
                [_fare(origin, dest, "2026-08-23", 30.0)],   # outbound hits
                [_fare(dest, origin, "2026-09-08", 30.0)],   # inbound: wrong date, no hit
            )

    deal = _wizz_deal(price=90.0)
    confirm_mod.confirm([deal], wizz=_HalfHitWizz())

    assert deal["price_eur"] == 90.0
    assert deal["price_confidence"] == "approximate"
    assert "estimated_price_eur" not in deal


def test_confirm_requery_exception_is_swallowed_estimate_kept():
    """confirm() is best-effort: a re-query exception is logged, never fatal,
    and never leaves the deal half-mutated."""
    deal = _wizz_deal(price=90.0)
    confirm_mod.confirm([deal], wizz=_RaisingWizz())

    assert deal["price_eur"] == 90.0
    assert deal["price_confidence"] == "approximate"
    assert "estimated_price_eur" not in deal


def test_confirm_exact_ryanair_deal_is_never_touched():
    deal = {
        "deal_id": "xyz9999999", "shape": "S2", "origin": "BUD", "destination": "CFU",
        "out_date": "2026-08-23", "return_date": "2026-08-29",
        "price_eur": 120.0, "price_confidence": "exact", "carriers": ["ryanair"], "legs": [],
    }
    confirm_mod.confirm([deal], wizz=_RaisingWizz())  # would raise if ever called
    assert deal["price_eur"] == 120.0
    assert "estimated_price_eur" not in deal
