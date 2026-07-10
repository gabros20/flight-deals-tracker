"""Minimal atomic state helpers (Global Constraint 7).

The one implementation of "write state without leaving a half-written file"
(tmp + ``os.replace``) and "append one observation atomically" (a single
``O_APPEND`` write, which the OS guarantees is atomic for a line below
``PIPE_BUF``). Task 8 extends this module with an atomic YAML writer, a
versioned reader (``schema_version`` gate + a friendly migration error) and an
``flock`` guard that makes ``brief`` single-instance (UPGRADE-PLAN §6).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import yaml

from flight_deals.paths import resolve_path

SCHEMA_VERSION = 1


class MigrationError(RuntimeError):
    """A state file was written by a newer/older ``schema_version`` than this
    code understands. Carries a human-readable message telling the operator
    what to do rather than crashing with a cryptic ``KeyError`` downstream."""


def atomic_write_json(path: Path | str, data: Dict[str, Any], *, schema_version: int = SCHEMA_VERSION) -> None:
    """Write ``data`` as JSON atomically (tmp file + ``os.replace``). A
    ``schema_version`` key is injected if absent (Global Constraint 7)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("schema_version", schema_version)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def atomic_write_yaml(path: Path | str, data: Dict[str, Any], *, schema_version: int = SCHEMA_VERSION) -> None:
    """Write ``data`` as YAML atomically (tmp + ``os.replace``). A
    ``schema_version`` key is injected if absent, mirroring
    :func:`atomic_write_json` — saved searches (SEARCH-DESIGN §4) are YAML so a
    human can read/edit them, but they still carry a version stamp."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("schema_version", schema_version)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


def read_versioned(
    path: Path | str,
    *,
    current: int = SCHEMA_VERSION,
    default: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Read a JSON or YAML state file (by suffix) and enforce its
    ``schema_version``.

    * Missing file -> ``default`` (``None`` unless given) — a not-yet-created
      state file is not an error.
    * ``schema_version`` newer than this code's ``current`` -> :class:`MigrationError`
      with an actionable message (the data was written by a newer build).
    * ``schema_version`` older with no migration path -> :class:`MigrationError`
      (there is only v1 today, so any mismatch is surfaced rather than guessed).

    Returns the parsed dict (including its ``schema_version``)."""
    path = Path(path)
    if not path.exists():
        return default
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise MigrationError(f"{path} is not a mapping — cannot read as versioned state")
    version = data.get("schema_version", current)
    if version != current:
        raise MigrationError(
            f"{path.name} is schema_version {version}, but this build understands "
            f"version {current}. Back up and remove the file to let it be recreated, "
            f"or run a matching build."
        )
    return data


@contextmanager
def flock_guard(name: str) -> Iterator[Path]:
    """Single-instance guard (UPGRADE-PLAN §6): take an exclusive, non-blocking
    ``flock`` on ``data/locks/<name>.lock``. A second concurrent holder cannot
    acquire it, prints ``<name>: already running`` to stderr and exits 1 — this
    is what stops an hourly ``brief`` from stacking on a slow run.

    The lock is advisory and process-scoped; it releases automatically when the
    fd closes (including on crash), so a stale lock never wedges the tool."""
    lock_path = resolve_path(f"data/locks/{name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            print(f"{name}: already running", file=sys.stderr)
            raise SystemExit(1)
        raise
    try:
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def append_jsonl(path: Path | str, record: Dict[str, Any]) -> None:
    """Append one JSON record as a single line. Uses a single ``O_APPEND``
    write so concurrent appenders never interleave a partial line (the append
    is atomic for a write below the OS pipe buffer, which one JSON record is).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def read_jsonl(path: Path | str) -> List[Dict[str, Any]]:
    """Read a JSONL file into a list of records. Missing file -> ``[]``; a
    malformed line is skipped (logged by the caller if it cares), never fatal."""
    path = Path(path)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
