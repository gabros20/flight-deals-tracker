import json

import pytest
import requests
from typer.testing import CliRunner

from flight_deals.cli import app

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "usage" in result.output.lower() or "help" in result.output.lower()


def _blow_up(*args, **kwargs):
    raise AssertionError("network I/O attempted during --help")


def test_help_performs_no_network_io(monkeypatch):
    """
    `--help` must never touch the network. Regression for the bug where
    `orchestrator = DealOrchestrator()` at module import time constructed a
    WizzProvider, which sniffs the current API version over HTTP on
    construction — meaning `flight-deals --help` silently made a network
    call. Orchestrator/notifier are now lazy (see cli.get_orchestrator()).
    """
    monkeypatch.setattr(requests.Session, "request", _blow_up)
    monkeypatch.setattr(requests, "get", _blow_up)
    monkeypatch.setattr(requests, "post", _blow_up)

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0

    # Sanity-check the guard itself is wired up correctly.
    with pytest.raises(AssertionError):
        requests.get("https://example.com")


REMOVED_STUBS = [
    ["roundtrip"],
    ["collect"],
    ["alerts"],
    ["history"],
    ["multi-airports"],
    ["search", "-c", "seaside", "--date-from", "2026-08-01", "--date-to", "2026-08-05", "--connections"],
    ["search", "-c", "seaside", "--date-from", "2026-08-01", "--date-to", "2026-08-05",
     "--return-from", "2026-08-10", "--return-to", "2026-08-12"],
]


@pytest.mark.parametrize("args", REMOVED_STUBS, ids=lambda a: " ".join(a))
def test_removed_commands_exit_2_with_json_error(args):
    """Removed/broken surface (Phase 0) must error honestly, not crash or fake a result."""
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    payload = json.loads(result.output.strip().splitlines()[0])
    assert payload == {"error": "removed_pending_rebuild", "hint": "see docs/UPGRADE-PLAN.md"}


# --- Task 7 intent verbs: offline validation paths (no network) ------------- #
def test_getaway_past_date_exits_2_with_hint():
    result = runner.invoke(app, ["getaway", "--depart", "2020-01-01..2020-01-05",
                                 "--where", "seaside", "--nights", "5-8"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] and "hint" in env


def test_getaway_bad_origin_exits_2_suggesting_iata():
    result = runner.invoke(app, ["getaway", "--depart", "2026-08-22..2026-08-24",
                                 "--where", "seaside", "--nights", "5-8", "--from", "BUDA"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert "BUD" in env["hint"]


def test_getaway_unknown_where_tag_empty_destinations_exits_2_no_network(monkeypatch):
    """Review item: `getaway --where "seasid & italy"` used to silently exit 0
    with route_status no_service AND burn a live Ryanair RT-ANYWHERE call
    before reporting it. It must now exit 2 with a did-you-mean hint and
    touch the network NOT AT ALL."""
    monkeypatch.setattr(requests.Session, "request", _blow_up)
    result = runner.invoke(app, ["getaway", "--depart", "2026-08-22..2026-08-24",
                                 "--where", "seasid & italy", "--nights", "5-8"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert "seaside" in env["hint"]


def test_getaway_legit_empty_where_category_exits_0_no_match_no_network(monkeypatch):
    """"ski" is a real tag matching zero BUD-reachable destinations today — a
    legitimately empty category, not a typo. Exit 0, no_match, no network."""
    monkeypatch.setattr(requests.Session, "request", _blow_up)
    result = runner.invoke(app, ["getaway", "--depart", "2026-08-22..2026-08-24",
                                 "--where", "ski", "--nights", "5-8"])
    assert result.exit_code == 0
    env = json.loads(result.output)
    assert env["route_status"] == "no_match"


def test_check_unknown_deal_exits_2():
    result = runner.invoke(app, ["check", "0000nope00"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] == "unknown_deal"


def test_getaway_and_oneway_and_check_registered():
    out = runner.invoke(app, ["--help"]).output.lower()
    assert "getaway" in out and "oneway" in out and "check" in out


# --- `search` is a TRUE alias of `oneway` (Task 7 fix wave) ----------------- #
# The legacy Rich-text output path is retired: `search` now runs the same
# intents pipeline as `oneway` (--category -> --where, --from -> origins,
# --date-from/--date-to -> --depart window, --max-price -> --budget) and
# always prints the standard JSON envelope (CONTRACT §1), never Rich text.
def test_search_past_date_exits_2_with_hint():
    result = runner.invoke(app, ["search", "-c", "seaside", "--date-from", "2020-01-01",
                                  "--date-to", "2020-01-05"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert env["error"] and "hint" in env


def test_search_bad_origin_exits_2_suggesting_iata():
    result = runner.invoke(app, ["search", "-c", "seaside", "--date-from", "2026-08-22",
                                  "--date-to", "2026-08-24", "--from", "BUDA"])
    assert result.exit_code == 2
    env = json.loads(result.output)
    assert "BUD" in env["hint"]


def test_search_help_notes_deprecation_and_kept_for_compat():
    out = runner.invoke(app, ["search", "--help"]).output.lower()
    assert "oneway" in out or "getaway" in out
    assert "backward compat" in out
