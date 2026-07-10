"""Golden tests for the planner: two committed spec files compile to committed
plan JSON byte-for-byte, and run (against fixture-mocked providers) to a
committed envelope. Everything is sorted deterministically so the goldens are
stable. Regenerate with ``FD_REGEN_GOLDENS=1 pytest tests/test_planner_golden.py``.
"""

import json
import os
from pathlib import Path

import pytest

from flight_deals.engine.planner import Planner, compile_plan
from flight_deals.engine.spec import parse_spec
from flight_deals.providers.ryanair import RyanairProvider

GOLDENS = Path(__file__).parent / "goldens"
FIXTURES = Path(__file__).parent / "fixtures"

SPECS = ["single_dest", "category_anywhere"]


def _load_spec(name: str):
    return parse_spec(json.loads((GOLDENS / f"spec_{name}.json").read_text()))


def _mocked_planner() -> Planner:
    """A planner whose providers return fixture data deterministically:
    Ryanair RT-ANYWHERE echoes the captured BUD anywhere sweep; Wizz serves no
    route (empty TT) — so the goldens exercise the exact-fare path + the
    'provider queried, returned nothing' sources case without live network."""
    body = json.loads((FIXTURES / "farfnd_roundtrip_anywhere_bud.json").read_text())["body"]

    planner = Planner()

    def fake_roundtrip(origin, dest=None, **kwargs):
        return RyanairProvider()._parse_roundtrip(
            body, kwargs.get("duration_from"), kwargs.get("duration_to")
        )

    planner.ryanair.roundtrip_fares = fake_roundtrip
    planner.wizz.timetable = lambda *a, **k: ([], [])
    return planner


def _regen() -> bool:
    return os.environ.get("FD_REGEN_GOLDENS") == "1"


@pytest.mark.parametrize("name", SPECS)
def test_plan_golden(name):
    spec = _load_spec(name)
    plan_dict = compile_plan(spec).to_dict()
    golden = GOLDENS / f"plan_{name}.json"
    if _regen():
        golden.write_text(json.dumps(plan_dict, indent=2) + "\n")
    expected = json.loads(golden.read_text())
    assert plan_dict == expected


@pytest.mark.parametrize("name", SPECS)
def test_run_golden(name):
    spec = _load_spec(name)
    planner = _mocked_planner()
    env, exit_code = planner.run(spec)
    golden = GOLDENS / f"envelope_{name}.json"
    if _regen():
        golden.write_text(json.dumps({"exit_code": exit_code, "envelope": env}, indent=2) + "\n")
    expected = json.loads(golden.read_text())
    assert exit_code == expected["exit_code"]
    assert env == expected["envelope"]


def test_run_golden_single_dest_is_one_exact_deal():
    """Sanity anchor for the single-dest golden (so a silent golden regen can't
    hide a regression): exactly one exact-confidence deal, and its deal_id is
    the frozen derivation."""
    from flight_deals.output import deal_id

    env, code = _mocked_planner().run(_load_spec("single_dest"))
    assert code == 0
    assert len(env["results"]) == 1
    d = env["results"][0]
    assert d["price_confidence"] == "exact"
    assert d["deal_id"] == deal_id(
        d["origin"], d["destination"], d["out_date"], d["return_date"], "S2", ["ryanair"]
    )
    # route_status MUST be absent when results are non-empty (CONTRACT invariant).
    assert "route_status" not in env
