"""DEPRECATED compatibility shim.

Earlier versions of this skill (pre agent-first-v2) exposed Python functions
here that imported the Typer app in-process (via `typer.testing.CliRunner`)
so Hermes could call them directly instead of shelling out. That coupling is
gone: this shim now delegates to the installed `flight-deals` CLI via
`subprocess`, so it keeps working even when the skill directory and the
project's Python environment (and its dependencies) diverge.

New integrations should not use this file at all — read `SKILL.md` and run
`flight-deals` directly; every command is a single JSON object on stdout
(`docs/CONTRACT.md`). This module exists only so an old caller that still
imports `wrapper.search_deals(...)` etc. doesn't hard-break.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

# Resolved lazily by relying on $PATH — set to an absolute path (e.g. the
# project venv's `bin/flight-deals`) if the CLI isn't on PATH in your
# environment. See references/project-venv-setup.md style notes in this
# directory's own references/ for the common failure mode (broken venv
# shebang after a project move).
CLI = "flight-deals"


def _run(args: list[str]) -> str:
    result = subprocess.run([CLI, *args], capture_output=True, text=True)
    return result.stdout


def search_deals(
    category: str,
    origin: str,
    date_from: str,
    date_to: str,
    max_price: Optional[float] = None,
    **_ignored,
) -> str:
    """DEPRECATED: use `flight-deals oneway --where <expr> --from <origin>
    --depart <date_from>..<date_to> [--budget N]` directly — see SKILL.md."""
    args = [
        "oneway", "--where", category, "--from", origin,
        "--depart", f"{date_from}..{date_to}",
    ]
    if max_price is not None:
        args += ["--budget", str(max_price)]
    return _run(args)


def track_route(origin: str, destination: str, month: str, max_price: float) -> str:
    """DEPRECATED: `track`'s percentage-drop tracking no longer exists — use
    `flight-deals watch add <origin>-<destination> --months <month> --max-price
    <N>` (an absolute EUR threshold) plus `flight-deals brief` on a schedule."""
    args = ["watch", "add", f"{origin}-{destination}", "--months", month,
            "--max-price", str(max_price)]
    return _run(args)


def list_destinations(tag: Optional[str] = None) -> str:
    """DEPRECATED: use `flight-deals where list` / `flight-deals where show
    "<expr>"` directly."""
    args = ["where", "show", tag] if tag else ["where", "list"]
    return _run(args)


def get_brief(pretty: bool = False) -> str:
    """DEPRECATED: use `flight-deals brief` directly."""
    args = ["brief"] + (["--pretty"] if pretty else [])
    return _run(args)


if __name__ == "__main__":  # pragma: no cover — manual smoke check only
    print(json.dumps({"deprecated": True, "hint": "read SKILL.md; call flight-deals directly"}))
