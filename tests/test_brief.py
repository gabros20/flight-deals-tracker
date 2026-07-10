"""brief end-to-end (Task 8 req 4/7) on fixture-mocked providers with a fake
notifier: a watch fires exactly one alert; the second run (same price) fires
nothing and would send nothing. Also covers movers, history-collect, and prune.
"""

from datetime import date, datetime, timezone

import pytest

from flight_deals.engine import brief as brief_mod
from flight_deals.engine.planner import Planner
from flight_deals.history import PriceHistoryStore
from flight_deals.http import ProviderDown
from flight_deals.models import FareLeg, FarePair
from flight_deals.state import alert_state, searches, snapshots

NOW = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 1)


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def send(self, text, *, dry_run=False, parse_mode="HTML"):
        self.calls.append(text)
        return True


def _farepair(dest, price, out="2026-08-20", ret="2026-08-25"):
    nights = (date.fromisoformat(ret) - date.fromisoformat(out)).days
    return FarePair(
        origin="BUD", destination=dest, out_date=out, return_date=ret, nights=nights,
        total_price_eur=float(price), currency_original="EUR", price_confidence="exact",
        carrier="ryanair", source_endpoint="farfnd/roundTripFares",
        outbound=FareLeg(origin="BUD", destination=dest, date=out, price_eur=price / 2, carrier="ryanair"),
        inbound=FareLeg(origin=dest, destination="BUD", date=ret, price_eur=price / 2, carrier="ryanair"),
    )


def _planner(fares_by_run):
    """A planner whose Ryanair RT-ANYWHERE returns a scripted list of FarePairs
    (one entry consumed per call), Wizz serves nothing."""
    planner = Planner()
    seq = list(fares_by_run)

    def _rt(origin, dest=None, **kw):
        return seq.pop(0) if seq else []

    planner.ryanair.roundtrip_fares = _rt
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    sdir = tmp_path / "searches"; sdir.mkdir()
    ddir = tmp_path / "deals"; ddir.mkdir()
    monkeypatch.setattr(searches, "searches_dir", lambda: sdir)
    monkeypatch.setattr(snapshots, "_deals_dir", lambda: ddir)
    hist = PriceHistoryStore(csv_path=str(tmp_path / "history.csv"))
    machine_path = tmp_path / "alert_state.json"
    return {"tmp": tmp_path, "history": hist, "machine_path": machine_path}


def _run(env_dirs, planner, **kw):
    machine = alert_state.AlertMachine(path=env_dirs["machine_path"], realert_drop_pct=15.0)
    return brief_mod.run_brief(
        force_all=True, now=NOW, today=TODAY, planner=planner,
        history_store=env_dirs["history"], alert_machine=machine, do_prune=False, **kw,
    )


# --------------------------------------------------------------------------- #
def test_watch_fires_exactly_one_alert_across_two_runs(env_dirs):
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30", alert={"max_price": 150, "notify": "telegram"})
    notifier = FakeNotifier()

    # Run 1: price 140 under threshold -> one alert; a --send would message once.
    p1 = _planner([[_farepair("CFU", 140)]])
    r1 = _run(env_dirs, p1)
    assert len(r1.fired) == 1
    assert r1.fired[0]["destination"] == "CFU"
    if brief_mod.should_send(r1):
        notifier.send("digest")

    # Run 2: same price -> suppressed, no alert, and nothing worth sending.
    p2 = _planner([[_farepair("CFU", 140)]])
    r2 = _run(env_dirs, p2)
    assert r2.fired == []
    if brief_mod.should_send(r2):
        notifier.send("digest")

    assert len(notifier.calls) == 1  # exactly once across both runs


def test_further_drop_re_alerts(env_dirs):
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 alert={"max_price": 150, "notify": "telegram"})
    assert len(_run(env_dirs, _planner([[_farepair("CFU", 140)]])).fired) == 1
    # 118 is >=15% below 140 -> re-alert.
    assert len(_run(env_dirs, _planner([[_farepair("CFU", 118)]])).fired) == 1


def test_above_threshold_never_alerts(env_dirs):
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 alert={"max_price": 100, "notify": "telegram"})
    r = _run(env_dirs, _planner([[_farepair("CFU", 140)]]))
    assert r.fired == []
    # No alert, but the run still collected the observation into history.
    assert env_dirs["history"].get_route_stats("BUD", "CFU")["count"] == 1


def test_search_without_alert_collects_but_never_fires(env_dirs):
    searches.add(name="plain", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                     "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    r = _run(env_dirs, _planner([[_farepair("CFU", 50)]]))
    assert r.fired == []
    assert env_dirs["history"].get_route_stats("BUD", "CFU")["count"] == 1


def test_mover_surfaces_on_price_drop_without_alert(env_dirs):
    searches.add(name="plain", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                     "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    _run(env_dirs, _planner([[_farepair("CFU", 200)]]))  # first observation
    r2 = _run(env_dirs, _planner([[_farepair("CFU", 160)]]))  # dropped 40
    assert r2.fired == []
    assert r2.envelope["brief"]["movers"] == 1
    assert brief_mod.should_send(r2) is True


def test_prune_pass_clears_past_dated_and_stale_state(env_dirs, monkeypatch):
    import flight_deals.cache as cache_mod

    class _NoCache:
        def prune_expired(self):
            return 0
    monkeypatch.setattr(cache_mod, "ResponseCache", _NoCache)

    # A past-dated snapshot file + a stale run stamp that prune should remove.
    snapshots.snapshot({"deal_id": "old12345678", "shape": "S2", "origin": "BUD",
                        "destination": "ZZZ", "out_date": "2026-01-01", "return_date": None,
                        "price_eur": 99.0, "price_confidence": "exact", "carriers": ["ryanair"]},
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    searches.stamp_run("ghost", NOW)  # no file for 'ghost'

    searches.add(name="plain", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                     "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    machine = alert_state.AlertMachine(path=env_dirs["machine_path"])
    brief_mod.run_brief(force_all=True, now=NOW, today=TODAY, planner=_planner([[_farepair("CFU", 90)]]),
                        history_store=env_dirs["history"], alert_machine=machine, do_prune=True)

    assert snapshots.records("old12345678") == []      # past-dated snapshot pruned
    assert searches.last_run_at("ghost") is None         # stale run stamp pruned


def _boom(*_a, **_kw):
    raise ProviderDown("provider unavailable")


def test_all_providers_down_across_all_due_searches_exits_1(env_dirs):
    """A cron day where every executed search had zero ok sources (both
    Ryanair and Wizz down for both searches) must be red: exit 1, with
    error=provider_error + hint on the envelope (controller ruling)."""
    searches.add(name="bud-ath", spec={"origins": ["BUD"], "destinations": ["ATH"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    planner = Planner()
    planner.ryanair.roundtrip_fares = _boom
    planner.wizz.timetable = _boom

    r = _run(env_dirs, planner)
    assert r.ran == ["bud-ath", "bud-cfu"]  # both searches actually ran
    assert r.fired == []
    assert r.exit_code == 1
    assert r.envelope["error"] == "provider_error"
    assert r.envelope["hint"]


def test_one_search_fine_one_degraded_exits_0_with_coverage_caveat(env_dirs):
    """A quiet day where at least one search still had a usable source stays
    exit 0 — only an ALL-degraded day is red. The digest still names the gap
    (CONTRACT §3 partial coverage)."""
    searches.add(name="bud-ath", spec={"origins": ["BUD"], "destinations": ["ATH"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"})
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"})

    planner = Planner()
    calls = {"n": 0}

    def _rt(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:  # bud-ath (runs first, sorted by name) -> fine
            return [_farepair("ATH", 140)]
        raise ProviderDown("ryanair unavailable")  # bud-cfu -> degraded

    planner.ryanair.roundtrip_fares = _rt
    planner.wizz.timetable = _boom  # Wizz down throughout, doesn't matter for the ruling

    r = _run(env_dirs, planner)
    assert r.ran == ["bud-ath", "bud-cfu"]
    assert r.exit_code == 0
    assert "error" not in r.envelope
    assert "unavailable" in r.envelope["summary"]  # coverage caveat in the digest


def test_nothing_due_sends_nothing(env_dirs):
    r = brief_mod.run_brief(
        force_all=False, now=NOW, today=TODAY, planner=_planner([]),
        history_store=env_dirs["history"],
        alert_machine=alert_state.AlertMachine(path=env_dirs["machine_path"]),
        do_prune=False,
    )
    assert r.ran == [] and r.fired == []
    assert brief_mod.should_send(r) is False
    assert "no saved searches were due" in r.envelope["summary"]
