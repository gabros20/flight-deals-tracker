"""``wake`` (Task 9 req 4): bundles a saved search's spec + agent_prompt + last
persisted run + history context + allowed moves. The fixture test drives the
real production path — ``run_brief`` persists the last-result cache, then
``build_wake`` reads it back — rather than hand-constructing the cache file.
"""

from datetime import date, datetime, timezone

import pytest

from flight_deals.engine import brief as brief_mod
from flight_deals.engine import wake as wake_mod
from flight_deals.engine.planner import Planner
from flight_deals.history import PriceHistoryStore
from flight_deals.models import FareLeg, FarePair
from flight_deals.state import alert_state, searches, snapshots

NOW = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 1)


def _farepair(dest, price, out="2026-08-20", ret="2026-08-25"):
    nights = (date.fromisoformat(ret) - date.fromisoformat(out)).days
    return FarePair(
        origin="BUD", destination=dest, out_date=out, return_date=ret, nights=nights,
        total_price_eur=float(price), currency_original="EUR", price_confidence="exact",
        carrier="ryanair", source_endpoint="farfnd/roundTripFares",
        outbound=FareLeg(origin="BUD", destination=dest, date=out, price_eur=price / 2, carrier="ryanair"),
        inbound=FareLeg(origin=dest, destination="BUD", date=ret, price_eur=price / 2, carrier="ryanair"),
    )


def _planner(fares):
    planner = Planner()
    seq = list(fares)
    planner.ryanair.roundtrip_fares = lambda *a, **k: (seq.pop(0) if seq else [])
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    sdir = tmp_path / "searches"
    sdir.mkdir()
    ddir = tmp_path / "deals"
    ddir.mkdir()
    monkeypatch.setattr(searches, "searches_dir", lambda: sdir)
    monkeypatch.setattr(snapshots, "_deals_dir", lambda: ddir)
    hist = PriceHistoryStore(csv_path=str(tmp_path / "history.csv"))
    machine_path = tmp_path / "alert_state.json"
    return {"tmp": tmp_path, "history": hist, "machine_path": machine_path}


def _run_brief(env_dirs, planner):
    machine = alert_state.AlertMachine(path=env_dirs["machine_path"], realert_drop_pct=15.0)
    return brief_mod.run_brief(
        force_all=True, now=NOW, today=TODAY, planner=planner,
        history_store=env_dirs["history"], alert_machine=machine, do_prune=False,
    )


def test_wake_unknown_search_exits_2(env_dirs):
    env, code = wake_mod.build_wake("does-not-exist", history_store=env_dirs["history"])
    assert code == 2
    assert env["error"] == "unknown_search"
    assert "searches list" in env["hint"]


def test_wake_never_run_reports_honestly(env_dirs):
    searches.add(
        name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                               "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
        schedule="daily 08:30", alert={"max_price": 150, "notify": "telegram"},
        agent_prompt="Weekly: try one variation and message only if worth it.",
    )
    env, code = wake_mod.build_wake("bud-cfu", history_store=env_dirs["history"])
    assert code == 0
    assert env["last_result"] is None
    assert env["results"] == []
    assert "never run" in env["summary"]
    assert env["agent_prompt"].startswith("Weekly:")
    assert env["spec"]["destinations"] == ["CFU"]
    assert env["allowed_moves"] == wake_mod.ALLOWED_MOVES
    assert any(m["move"] == "persist_variation" for m in env["allowed_moves"])


def test_wake_bundles_last_result_and_history_after_brief(env_dirs):
    searches.add(
        name="bud-cfu", spec={"origins": ["BUD"], "destinations": ["CFU"],
                               "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
        schedule="daily 08:30", alert={"max_price": 150, "notify": "telegram"},
        agent_prompt="Weekly: try one variation and message only if worth it.",
    )

    # First brief run establishes a history observation; second is what wake reads.
    _run_brief(env_dirs, _planner([[_farepair("CFU", 200)]]))
    _run_brief(env_dirs, _planner([[_farepair("CFU", 140)]]))

    env, code = wake_mod.build_wake("bud-cfu", history_store=env_dirs["history"])
    assert code == 0
    assert env["name"] == "bud-cfu"
    assert env["schedule"] == "daily 08:30"
    assert env["alert"] == {"max_price": 150, "notify": "telegram"}
    assert env["agent_prompt"].startswith("Weekly:")

    last = env["last_result"]
    assert last is not None and last["ran_at"]
    assert len(last["results"]) == 1
    assert last["results"][0]["destination"] == "CFU"
    assert "found 1 deal" in env["summary"]

    history = env["history"]
    assert len(history) == 1
    assert history[0]["origin"] == "BUD" and history[0]["destination"] == "CFU"
    assert history[0]["compare"]["count"] == 2  # both brief runs collected an observation

    assert env["allowed_moves"] == wake_mod.ALLOWED_MOVES
    assert env["next"] == ["flight-deals searches show bud-cfu"]


def test_searches_due_agentic_filters_to_agent_prompt_bearing(env_dirs):
    searches.add(name="plain", spec={"origins": ["BUD"], "destinations": ["ATH"],
                                     "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30")
    searches.add(name="agentic-one", spec={"origins": ["BUD"], "destinations": ["CFU"],
                                           "depart": "2026-08-20..2026-08-24", "nights": "5-7"},
                 schedule="daily 08:30", agent_prompt="Look for something interesting.")

    due_all = searches.due(NOW)
    assert {r["name"] for r in due_all} == {"plain", "agentic-one"}

    agentic = [r for r in due_all if r.get("agent_prompt")]
    assert [r["name"] for r in agentic] == ["agentic-one"]
