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
from flight_deals.state import alert_state, searches, snapshots, store

NOW = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 1)


class FakeNotifier:
    """Stand-in for the real notifier, injected into run_brief so the tests drive
    the PRODUCTION send path (item 1 acknowledged-send), not a re-implementation.
    ``ok=False`` simulates a transient Telegram failure."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []      # confirmed real sends
        self.previews = []  # dry-run previews (no network)

    def send(self, text, *, dry_run=False, parse_mode="HTML"):
        if dry_run:
            self.previews.append(text)
            return True
        self.sent.append(text)
        return self.ok


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
def _add_bud_cfu_watch():
    searches.add(name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30", alert={"max_price": 150, "notify": "telegram"})


def test_watch_sends_exactly_once_across_two_runs(env_dirs):
    """Drive the production send path: the notifier is injected into run_brief,
    fires once, and a suppressed second run sends nothing (exactly-once)."""
    _add_bud_cfu_watch()

    # Run 1: price 140 under threshold -> one alert, one confirmed send.
    r1 = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=FakeNotifier(), send=True)
    assert len(r1.fired) == 1 and r1.fired[0]["destination"] == "CFU"
    assert r1.sent is True

    # Run 2: same price -> suppressed, acknowledged already -> nothing sent.
    n2 = FakeNotifier()
    r2 = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=n2, send=True)
    assert r2.fired == []
    assert n2.sent == []


def test_failed_send_leaves_pending_and_next_run_resends(env_dirs):
    """Regression for the lost-alert bug: a send that returns False must NOT
    mark the alert sent, and the NEXT run re-includes and re-sends it — even
    though the price is unchanged and would otherwise be suppressed."""
    _add_bud_cfu_watch()

    # Run 1: alert fires but the send fails -> exit 1, entry left pending.
    failing = FakeNotifier(ok=False)
    r1 = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=failing, send=True)
    assert len(r1.fired) == 1
    assert len(failing.sent) == 1        # send WAS attempted
    assert r1.exit_code == 1             # a failed send still exits 1
    entry = alert_state.AlertMachine(path=env_dirs["machine_path"]).get("bud-cfu", "BUD-CFU", "2026-08")
    assert entry is not None and entry["sent"] is False

    # Run 2: same price (would be suppressed) but the pending entry is re-sent.
    ok = FakeNotifier()
    r2 = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=ok, send=True)
    assert len(r2.fired) == 1            # re-included despite suppression
    assert len(ok.sent) == 1            # re-sent, now acknowledged
    assert r2.exit_code == 0

    # Run 3: now acknowledged -> quiet.
    ok3 = FakeNotifier()
    r3 = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=ok3, send=True)
    assert r3.fired == []
    assert ok3.sent == []


def test_dry_run_previews_but_never_marks_sent(env_dirs):
    """A dry-run must send nothing over the wire and must leave the alert entry
    pending (sent=False) — it only previews the digest."""
    _add_bud_cfu_watch()
    notifier = FakeNotifier()
    r = _run(env_dirs, _planner([[_farepair("CFU", 140)]]), notifier=notifier, dry_run=True)
    assert len(r.fired) == 1
    assert notifier.sent == []          # nothing sent
    assert len(notifier.previews) == 1  # but a preview was produced
    entry = alert_state.AlertMachine(path=env_dirs["machine_path"]).get("bud-cfu", "BUD-CFU", "2026-08")
    assert entry is not None and entry["sent"] is False


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


def test_malformed_saved_searches_are_skipped_not_fatal(env_dirs):
    """A bad-schedule file, corrupt YAML, and a corrupt .runs.json alongside a
    healthy scheduled watch: the healthy one still runs, each bad file is skipped
    and surfaced in brief.searches_skipped, and the exit code is unaffected by
    the skips (item 2)."""
    sdir = searches.searches_dir()
    searches.add(name="healthy", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30", alert={"max_price": 150})
    # 1) Corrupt YAML — unterminated flow sequence.
    (sdir / "brokenyaml.yaml").write_text("spec: [1, 2, 3\n")
    # 2) Valid file but a malformed schedule string (bypass add's validation).
    store.atomic_write_yaml(sdir / "badsched.yaml", {
        "name": "badsched",
        "spec": {"origins": ["BUD"], "destinations": ["ATH"],
                 "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
        "schedule": "daily 99:99",
    })
    # 3) Corrupt scheduling cache — must be treated as empty, not crash.
    (sdir / ".runs.json").write_text("{ not json")

    r = brief_mod.run_brief(
        force_all=False, now=NOW, today=TODAY, planner=_planner([[_farepair("CFU", 140)]]),
        history_store=env_dirs["history"],
        alert_machine=alert_state.AlertMachine(path=env_dirs["machine_path"]),
        do_prune=False,
    )
    assert r.ran == ["healthy"]              # healthy search still ran
    assert len(r.fired) == 1
    skipped_files = {s["file"] for s in r.envelope["brief"]["searches_skipped"]}
    assert "brokenyaml.yaml" in skipped_files
    assert "badsched.yaml" in skipped_files
    assert r.exit_code == 0                  # a skip never changes the exit code


def test_gem_watch_alerts_on_extended_total(env_dirs):
    """A `watch add --to <gem>` must persist the gem on SearchSpec (Task 15b
    controller ruling) so `brief` — which only ever has the loaded spec, never
    the transient --to resolution an interactive run gets — replays it as the
    gem-only onward extension. The alert threshold has to apply to the
    fare+onward EXTENDED total, not the bare gateway fare, mirroring the
    interactive-path coverage in test_gems_engine.py::
    test_watch_threshold_fires_on_extended_total."""
    searches.add(
        name="halki-watch",
        spec={"origins": ["BUD"], "destinations": ["RHO"], "gem": "halki",
              "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
        schedule="daily 08:30", alert={"max_price": 130, "notify": "telegram"},
    )
    planner = Planner()
    planner.ryanair.roundtrip_fares = lambda origin, dest=None, **kw: [_farepair("RHO", 100.0)]
    planner.wizz.timetable = lambda *a, **k: ([], [])

    notifier = FakeNotifier()
    r = _run(env_dirs, planner, notifier=notifier, send=True)

    assert len(r.fired) == 1
    fired = r.fired[0]
    assert fired.get("onward", {}).get("gem") == "halki"
    assert fired["price_eur"] == 120.0        # 100 gateway fare + 20 onward (RHO, ×2 rt)
    assert notifier.sent                       # extended total (120) crossed the €130 cap
    assert r.exit_code == 0


def test_stale_gem_slug_in_saved_spec_skips_not_crash(env_dirs):
    """A gem removed from the catalog after `watch add --to <gem>` was saved
    must not crash `brief` — SearchSpec.gem re-validates on every parse_spec
    (Task 15b), so a since-removed slug surfaces as an invalid-spec skip, the
    same non-fatal handling already covering a bad schedule string or corrupt
    YAML (searches.py's malformed-search guard)."""
    sdir = searches.searches_dir()
    searches.add(name="healthy", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                       "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30", alert={"max_price": 150})
    # Bypass add()'s validation (which would reject this outright) to simulate
    # a spec that was valid when saved and went stale after the catalog changed.
    store.atomic_write_yaml(sdir / "stale-gem.yaml", {
        "schema_version": searches.SCHEMA_VERSION,
        "name": "stale-gem",
        "spec": {"origins": ["BUD"], "destinations": ["RHO"], "gem": "not-a-real-gem-slug",
                 "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
        "schedule": "daily 08:30",
        "alert": {"max_price": 150},
    })

    r = brief_mod.run_brief(
        force_all=True, now=NOW, today=TODAY, planner=_planner([[_farepair("CFU", 140)]]),
        history_store=env_dirs["history"],
        alert_machine=alert_state.AlertMachine(path=env_dirs["machine_path"]),
        do_prune=False,
    )
    assert r.ran == ["healthy"]     # the stale-gem search never ran, but didn't crash
    assert len(r.fired) == 1        # healthy watch alongside it still fired normally
    assert r.exit_code == 0         # a skip alongside a healthy run stays green


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
