"""Minimal atomic state helpers (Global Constraint 7).

The one implementation of "write state without leaving a half-written file"
(tmp + ``os.replace``) and "append one observation atomically" (a single
``O_APPEND`` write, which the OS guarantees is atomic for a line below
``PIPE_BUF``). Task 8 extends this module with flock guards, versioned reads
and migration errors; Task 7 only needs these three primitives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = 1


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
