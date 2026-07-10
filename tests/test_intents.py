"""Task 7 intent verbs: getaway/oneway builders, estimate→confirm, empty
states, validation, and the getaway envelope goldens.

Providers are fixture-mocked (no network); history is a deterministic stub so
the golden envelopes are byte-stable. Regenerate goldens with
``FD_REGEN_GOLDENS=1 pytest tests/test_intents.py``.
"""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from flight_deals.engine.intents import IntentError, check_deal, run_search
from flight_deals.engine.planner import Planner
from flight_deals.models import DayFare
from flight_deals.providers.ryanair import RyanairProvider

GOLDENS = Path(__file__).parent / "goldens"
FIXTURES = Path(__file__).parent / "fixtures"
FIXED_TODAY = date(2026, 1, 1)
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _FakeHistory:
    """No prior data -> every deal is 'baseline' with an insufficient-history
    why-string. Deterministic, so goldens don't depend on the project CSV."""

    def compare(self, origin, destination, price, window_days=None):
        return {"count": 0, "median": None, "min": None,
                "pct_vs_typical": None, "sufficient": False, "best_this_month": False}


def _collector():
    seen = []
    return seen, (lambda d, now=None: seen.append(d["deal_id"]))


def _anywhere_planner():
    """Ryanair RT-ANYWHERE echoes the captured BUD sweep; Wizz serves nothing."""
    body = json.loads((FIXTURES / "farfnd_roundtrip_anywhere_bud.json").read_text())["body"]
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda origin, dest=None, **kw: RyanairProvider()._parse_roundtrip(
        body, kw.get("duration_from"), kw.get("duration_to"))
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


def _regen():
    return os.environ.get("FD_REGEN_GOLDENS") == "1"


# --------------------------------------------------------------------------- #
# getaway envelope golden (exact Ryanair path)                                #
# --------------------------------------------------------------------------- #
def test_getaway_envelope_golden():
    seen, snap = _collector()
    env, code = run_search(
        where="croatia & seaside", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=_anywhere_planner(),
        history_store=_FakeHistory(), snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    golden = GOLDENS / "getaway_envelope.json"
    if _regen():
        golden.write_text(json.dumps({"exit_code": code, "envelope": env}, indent=2) + "\n")
    expected = json.loads(golden.read_text())
    assert code == expected["exit_code"]
    assert env == expected["envelope"]
    # every displayed deal was snapshotted
    assert seen == [d["deal_id"] for d in env["results"]]


def test_getaway_deals_are_exact_and_grouped_baseline_without_history():
    _, snap = _collector()
    env, code = run_search(
        where="croatia & seaside", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=_anywhere_planner(),
        history_store=_FakeHistory(), snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0 and env["results"]
    for d in env["results"]:
        assert d["price_confidence"] == "exact"
        assert d["group"] == "baseline"
        assert "insufficient history" in d["why"]
    assert "route_status" not in env


# --------------------------------------------------------------------------- #
# empty-state golden                                                          #
# --------------------------------------------------------------------------- #
def _empty_planner():
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda *a, **k: []
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


def test_getaway_empty_state_golden():
    _, snap = _collector()
    env, code = run_search(
        where="croatia & seaside", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], planner=_empty_planner(),
        history_store=_FakeHistory(), snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    golden = GOLDENS / "getaway_empty.json"
    if _regen():
        golden.write_text(json.dumps({"exit_code": code, "envelope": env}, indent=2) + "\n")
    expected = json.loads(golden.read_text())
    assert code == expected["exit_code"]
    assert env == expected["envelope"]
    assert env["route_status"] == "no_service"
    assert code == 0
    assert len(env["next"]) == 1  # exactly ONE widening move


# --------------------------------------------------------------------------- #
# estimate→confirm: the confirmed price differs from the windowed estimate    #
# --------------------------------------------------------------------------- #
def _df(origin, dest, day, price):
    return DayFare(origin=origin, destination=dest, date=day, price_eur=price,
                   currency_original="EUR", price_confidence="approximate",
                   carrier="wizzair", source_endpoint="wizz/timetable")


def test_confirm_replaces_estimate_with_exact_requery():
    """A Wizz deal's windowed estimate (€90) is re-checked on the exact dates
    with a cache-bypassed query that returns €70; the confirmed figure replaces
    price_eur and the estimate is retained in estimated_price_eur."""
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda *a, **k: []  # no exact competitor

    def fake_tt(origin, dest, date_from, date_to, use_cache=True):
        if not use_cache:  # confirm re-query on exact dates -> cheaper truth
            return ([_df("BUD", "CFU", "2026-08-23", 35.0)],
                    [_df("CFU", "BUD", "2026-08-29", 35.0)])
        # planner's windowed estimate
        return ([_df("BUD", "CFU", "2026-08-23", 45.0)],
                [_df("CFU", "BUD", "2026-08-29", 45.0)])

    planner.wizz.timetable = fake_tt
    _, snap = _collector()
    env, code = run_search(
        where="greece & seaside", depart="2026-08-22..2026-08-24", nights="5-8",
        budget=None, origins=["BUD"], carriers=["wizzair"], planner=planner,
        history_store=_FakeHistory(), snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0
    cfu = next(d for d in env["results"] if d["destination"] == "CFU")
    assert cfu["price_confidence"] == "approximate"  # Wizz stays approximate
    assert cfu["estimated_price_eur"] == 90.0        # 45 + 45 windowed estimate
    assert cfu["price_eur"] == 70.0                  # 35 + 35 confirmed
    assert cfu["legs"][0]["price_eur"] == 35.0


# --------------------------------------------------------------------------- #
# oneway (S1)                                                                 #
# --------------------------------------------------------------------------- #
def test_oneway_produces_s1_deals():
    planner = Planner()
    ow_body = {"fares": [{"outbound": {
        "departureAirport": {"iataCode": "BUD"}, "arrivalAirport": {"iataCode": "CFU"},
        "departureDate": "2026-08-23T10:00:00", "price": {"value": 24.99, "currencyCode": "EUR"},
        "flightNumber": "FR100"}}], "size": 1}
    planner.ryanair.oneway_fares = lambda origin, dest=None, **k: RyanairProvider()._parse_oneway(ow_body)
    planner.wizz.timetable = lambda *a, **k: ([], [])
    _, snap = _collector()
    env, code = run_search(
        where="greece & seaside", depart="2026-08-22..2026-08-24", nights=None,
        budget=None, origins=["BUD"], planner=planner, history_store=_FakeHistory(),
        snapshotter=snap, today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert code == 0
    d = next(x for x in env["results"] if x["destination"] == "CFU")
    assert d["shape"] == "S1"
    assert d["return_date"] is None and d["nights"] is None
    assert d["price_eur"] == 24.99


# --------------------------------------------------------------------------- #
# validation (before network)                                                #
# --------------------------------------------------------------------------- #
def test_getaway_rejects_past_departure():
    with pytest.raises(IntentError) as ei:
        run_search(where="greece", depart="2020-01-01..2020-01-05", nights="5-8",
                   budget=None, origins=["BUD"], planner=_empty_planner(),
                   history_store=_FakeHistory(), today=FIXED_TODAY)
    assert "before today" in ei.value.message


def test_getaway_fuzzy_matches_unknown_origin():
    with pytest.raises(IntentError) as ei:
        run_search(where="greece", depart="2026-08-22..2026-08-24", nights="5-8",
                   budget=None, origins=["BUDA"], planner=_empty_planner(),
                   history_store=_FakeHistory(), today=FIXED_TODAY)
    assert "BUD" in ei.value.hint


# --------------------------------------------------------------------------- #
# check <deal_id> round-trip                                                  #
# --------------------------------------------------------------------------- #
def test_check_unknown_deal_exits_2(tmp_path, monkeypatch):
    from flight_deals.state import snapshots
    monkeypatch.setattr(snapshots, "_deals_dir", lambda: tmp_path)
    env, code = check_deal("deadbeef00", today=FIXED_TODAY)
    assert code == 2
    assert env["error"] == "unknown_deal"


def test_check_roundtrip_reports_delta(tmp_path, monkeypatch):
    from flight_deals.state import snapshots

    monkeypatch.setattr(snapshots, "_deals_dir", lambda: tmp_path)
    # Seed a snapshot at €100 (an exact Ryanair round-trip).
    from flight_deals.output import build_deal, why_string
    deal = build_deal(shape="S2", origin="BUD", destination="CFU", out_date="2026-08-23",
                      return_date="2026-08-29", price_eur=100.0, price_confidence="exact",
                      carriers=["ryanair"], legs=[], why="seed")
    snapshots.snapshot(deal, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    # A planner whose live exact re-query now returns €80.
    from flight_deals.models import FareLeg, FarePair
    planner = Planner()

    def fake_rt(origin, dest, **k):
        leg_o = FareLeg(origin="BUD", destination="CFU", date="2026-08-23", price_eur=40.0, carrier="ryanair")
        leg_i = FareLeg(origin="CFU", destination="BUD", date="2026-08-29", price_eur=40.0, carrier="ryanair")
        return [FarePair(origin="BUD", destination="CFU", out_date="2026-08-23", return_date="2026-08-29",
                         nights=6, total_price_eur=80.0, currency_original="EUR", price_confidence="exact",
                         carrier="ryanair", source_endpoint="farfnd/roundTripFares", outbound=leg_o, inbound=leg_i)]

    planner.ryanair.roundtrip_fares = fake_rt
    env, code = check_deal(deal["deal_id"], planner=planner, today=FIXED_TODAY,
                           now=datetime(2026, 6, 2, tzinfo=timezone.utc))
    assert code == 0
    assert env["results"][0]["price_eur"] == 80.0
    assert env["delta"]["delta_vs_last_eur"] == -20.0
    assert env["delta"]["last_price_eur"] == 100.0
    # a new observation was appended (two total now)
    assert len(snapshots.records(deal["deal_id"])) == 2


def test_check_past_dated_deal_exits_2(tmp_path, monkeypatch):
    from flight_deals.state import snapshots
    from flight_deals.output import build_deal

    monkeypatch.setattr(snapshots, "_deals_dir", lambda: tmp_path)
    deal = build_deal(shape="S2", origin="BUD", destination="CFU", out_date="2026-08-23",
                      return_date="2026-08-29", price_eur=100.0, price_confidence="exact",
                      carriers=["ryanair"], legs=[], why="seed")
    snapshots.snapshot(deal, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    # "today" is after the departure date.
    env, code = check_deal(deal["deal_id"], today=date(2026, 9, 1))
    assert code == 2
    assert env["error"] == "dates_passed"
    assert "getaway" in env["hint"]
