"""Drift-pin: every command the router (SKILL.md) and the evals promise must
still resolve against the *actual* Typer/click command registry (I3).

This test parses the `flight-deals …` invocations documented in
``skills/flight-deals/SKILL.md`` (the intent-table Run column, inline spans,
and the worked-example fence) and each ``expected_command`` in
``evals/cases/*.yaml``, extracts the verb path + ``--flags``, and asserts every
one is a real registered command / option. It FAILS if a documented verb or
flag is renamed or removed — catching doc/CLI drift before an agent runs a
command that no longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
import yaml

from flight_deals.cli import app

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "skills" / "flight-deals" / "SKILL.md"
CASES_DIR = ROOT / "evals" / "cases"

# Each documented invocation begins "flight-deals …" and runs to the end of the
# line or the closing inline-code backtick.
_INVOCATION = re.compile(r"flight-deals[ \t]+([^\n`]*)")
_FLAG = re.compile(r"--[a-zA-Z][\w-]*")


def _cli_group():
    return typer.main.get_command(app)


def _resolve_command(tokens: list[str]):
    """Descend the click group tree consuming leading command tokens. Returns
    ``(command, consumed)`` — ``consumed == 0`` means the first token is not a
    registered verb at all."""
    cmd = _cli_group()
    root = cmd
    consumed = 0
    while (
        consumed < len(tokens)
        and hasattr(cmd, "commands")
        and tokens[consumed] in cmd.commands
    ):
        cmd = cmd.commands[tokens[consumed]]
        consumed += 1
    return (None if cmd is root else cmd), consumed


def _option_names(command) -> set[str]:
    names: set[str] = set()
    for p in command.params:
        names.update(p.opts)
        names.update(p.secondary_opts)
    return names


def _documented_invocations() -> list[str]:
    text = SKILL_MD.read_text()
    return [m.group(1).strip() for m in _INVOCATION.finditer(text)]


def _eval_invocations() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        cmd = (data or {}).get("expected_command")
        if not cmd:  # ambiguous cases carry expected_behavior instead
            continue
        for m in _INVOCATION.finditer(cmd):
            out.append((path.name, m.group(1).strip()))
    return out


def _assert_resolves(source: str, invocation: str) -> None:
    tokens = invocation.split()
    assert tokens, f"{source}: empty invocation"
    command, consumed = _resolve_command(tokens)
    assert command is not None, (
        f"{source}: '{tokens[0]}' is not a registered flight-deals command "
        f"(invocation: flight-deals {invocation})"
    )
    valid = _option_names(command)
    for flag in _FLAG.findall(invocation):
        assert flag in valid, (
            f"{source}: '{flag}' is not an option of "
            f"'{' '.join(tokens[:consumed])}' (valid: {sorted(valid)})"
        )


def test_skill_md_documents_at_least_the_core_verbs():
    invs = _documented_invocations()
    verbs = {inv.split()[0] for inv in invs if inv.split()}
    # Guardrail: if the parser silently stops matching, this catches it.
    for core in ("getaway", "oneway", "brief", "check"):
        assert core in verbs, f"SKILL.md no longer documents '{core}'"


def test_skill_md_invocations_resolve_against_cli():
    for inv in _documented_invocations():
        _assert_resolves("SKILL.md", inv)


def test_eval_expected_commands_resolve_against_cli():
    cases = _eval_invocations()
    assert cases, "no eval expected_command invocations found"
    for name, inv in cases:
        _assert_resolves(f"evals/cases/{name}", inv)
