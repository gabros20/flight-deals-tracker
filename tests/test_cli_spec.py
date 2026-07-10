"""CLI plan/run + the two binding CONTRACT invariants (Task 2/6 review):
route_status absent when results non-empty; provider_error pairs with exit 1."""

import json
from pathlib import Path

import requests
from typer.testing import CliRunner

from flight_deals.cli import app
from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"

SEASIDE_SINGLE = ('{"origins":["BUD"],"where":"greece & seaside",'
                  '"depart":"2026-08-22..2026-08-24","nights":"5-8"}')


def _mock_anywhere(monkeypatch):
    body = json.loads((FIXTURES / "farfnd_roundtrip_anywhere_bud.json").read_text())["body"]
    monkeypatch.setattr(
        RyanairProvider, "roundtrip_fares",
        lambda self, origin, dest=None, **k: self._parse_roundtrip(
            body, k.get("duration_from"), k.get("duration_to")),
    )
    monkeypatch.setattr(WizzProvider, "timetable", lambda self, *a, **k: ([], []))


# --- plan: no network ------------------------------------------------------ #
def test_plan_makes_no_network_call(monkeypatch):
    def blow_up(*a, **k):
        raise AssertionError("plan made a network call")
    monkeypatch.setattr(requests.Session, "request", blow_up)

    result = runner.invoke(app, ["plan", "--spec", SEASIDE_SINGLE])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["estimated_calls"] >= 1
    assert payload["calls"][0]["mode"] == "anywhere"


# --- run: binding invariant 1 (route_status absent when non-empty) --------- #
def test_run_nonempty_has_no_route_status(monkeypatch):
    _mock_anywhere(monkeypatch)
    result = runner.invoke(app, ["run", "--spec", SEASIDE_SINGLE])
    assert result.exit_code == 0
    env = json.loads(result.output)
    assert len(env["results"]) >= 1
    assert "route_status" not in env  # frozen invariant


# --- run: binding invariant 2 (provider_error pairs with exit 1) ----------- #
def test_run_provider_error_exits_1_with_route_status(monkeypatch):
    from flight_deals.http import ProviderDown

    def boom(self, *a, **k):
        raise ProviderDown("simulated outage")
    monkeypatch.setattr(RyanairProvider, "roundtrip_fares", boom)
    monkeypatch.setattr(WizzProvider, "timetable", lambda self, *a, **k: ([], []))

    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"where":"sicily",'
                                 '"depart":"2026-08-22..2026-08-24","nights":"5-8"}'])
    assert result.exit_code == 1
    env = json.loads(result.output)
    assert env["results"] == []
    assert env["route_status"] == "provider_error"
    # CONTRACT §1/§3: exit 1 MUST carry error+hint (violation this test used
    # to miss — it only checked route_status).
    assert env["error"] == "provider_error"
    assert env["hint"]


# --- run: empty-but-ok stays exit 0 with a typed empty state --------------- #
def test_run_empty_no_failure_is_exit_0_no_service(monkeypatch):
    monkeypatch.setattr(RyanairProvider, "roundtrip_fares", lambda self, *a, **k: [])
    monkeypatch.setattr(WizzProvider, "timetable", lambda self, *a, **k: ([], []))
    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"where":"sicily",'
                                 '"depart":"2026-08-22..2026-08-24","nights":"5-8"}'])
    assert result.exit_code == 0
    env = json.loads(result.output)
    assert env["route_status"] == "no_service"


# --- spec validation errors -> exit 2 + hint ------------------------------- #
def test_run_bad_depart_exits_2_with_hint():
    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"depart":"whenever","nights":"5-8"}'])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] and env["hint"]


def test_run_disabled_shape_exits_2_with_hint():
    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"where":"seaside","depart":"2026-08",'
                                 '"nights":"5-8","shapes":["via-hub"]}'])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"]
    assert "not yet enabled" in env["hint"]


def test_run_over_max_calls_exits_2_with_narrow_hint():
    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"where":"seaside",'
                                 '"depart":"2026-08-22..2026-08-24","nights":"5-8"}',
                                 "--max-calls", "5"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"]
    assert "--max-calls" in env["hint"]


# --- where-parse errors through plan/run -> exit 2 + error/hint ----------- #
def test_plan_invalid_where_exits_2_with_error_and_hint():
    result = runner.invoke(app, ["plan", "--spec",
                                 '{"origins":["BUD"],"where":"seasid & (",'
                                 '"depart":"2026-08-22..2026-08-24","nights":"5-8"}'])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] and env["hint"]


def test_run_invalid_where_exits_2_with_error_and_hint():
    result = runner.invoke(app, ["run", "--spec",
                                 '{"origins":["BUD"],"where":"seasid & (",'
                                 '"depart":"2026-08-22..2026-08-24","nights":"5-8"}'])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] and env["hint"]



# --- partial coverage: a provider failing while another succeeds is exit 0 -
# (controller-amended CONTRACT §3: only empty-AND-failed is exit 1) -------- #
def test_run_partial_coverage_is_exit_0_with_incomplete_summary(monkeypatch):
    from flight_deals.http import ProviderDown

    body = json.loads((FIXTURES / "farfnd_roundtrip_anywhere_bud.json").read_text())["body"]
    monkeypatch.setattr(
        RyanairProvider, "roundtrip_fares",
        lambda self, origin, dest=None, **k: self._parse_roundtrip(
            body, k.get("duration_from"), k.get("duration_to")),
    )

    def wizz_boom(self, *a, **k):
        raise ProviderDown("wizz simulated outage")
    monkeypatch.setattr(WizzProvider, "timetable", wizz_boom)

    result = runner.invoke(app, ["run", "--spec", SEASIDE_SINGLE])
    assert result.exit_code == 0
    env = json.loads(result.output)
    assert len(env["results"]) >= 1
    assert "route_status" not in env
    assert "error" not in env and "hint" not in env
    assert env["sources"]["wizzair"] == "error"
    assert "incomplete" in env["summary"]

def test_plan_inline_and_stdin_equivalent(monkeypatch):
    inline = runner.invoke(app, ["plan", "--spec", SEASIDE_SINGLE])
    piped = runner.invoke(app, ["plan", "--spec", "-"], input=SEASIDE_SINGLE)
    assert inline.exit_code == piped.exit_code == 0
    assert json.loads(inline.output) == json.loads(piped.output)
