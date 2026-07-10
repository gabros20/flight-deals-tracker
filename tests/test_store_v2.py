"""state/store.py Task-8 extensions: atomic YAML, versioned reads + migration
errors, concurrent-write safety, and flock exclusivity (via a real subprocess).
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from multiprocessing import Process

import pytest
import yaml

from flight_deals.state import store


def test_atomic_write_yaml_roundtrip_and_schema_version(tmp_path):
    p = tmp_path / "s.yaml"
    store.atomic_write_yaml(p, {"name": "x", "spec": {"origins": ["BUD"]}})
    data = yaml.safe_load(p.read_text())
    assert data["name"] == "x" and data["schema_version"] == 1
    assert list(tmp_path.glob("*.tmp*")) == []


def test_read_versioned_missing_returns_default(tmp_path):
    assert store.read_versioned(tmp_path / "nope.json") is None
    assert store.read_versioned(tmp_path / "nope.json", default={"a": 1}) == {"a": 1}


def test_read_versioned_reads_json_and_yaml(tmp_path):
    j = tmp_path / "a.json"
    store.atomic_write_json(j, {"a": 1})
    assert store.read_versioned(j)["a"] == 1
    y = tmp_path / "b.yaml"
    store.atomic_write_yaml(y, {"b": 2})
    assert store.read_versioned(y)["b"] == 2


def test_read_versioned_raises_migration_error_on_version_mismatch(tmp_path):
    p = tmp_path / "future.json"
    p.write_text(json.dumps({"schema_version": 99, "data": 1}))
    with pytest.raises(store.MigrationError) as ei:
        store.read_versioned(p, current=1)
    assert "schema_version 99" in str(ei.value)


def _writer(path, value):
    for _ in range(40):
        store.atomic_write_json(path, {"writer": value, "payload": [value] * 50})


def test_concurrent_atomic_writes_never_corrupt(tmp_path):
    """Many processes hammering one file with atomic writes must always leave a
    fully-parseable file (tmp + os.replace guarantees no torn reads)."""
    path = tmp_path / "hot.json"
    store.atomic_write_json(path, {"writer": 0, "payload": []})
    procs = [Process(target=_writer, args=(path, i)) for i in range(1, 6)]
    for pr in procs:
        pr.start()
    # Interleave reads while writers run; each read must fully parse.
    for _ in range(200):
        data = json.loads(path.read_text())
        assert "writer" in data and isinstance(data["payload"], list)
    for pr in procs:
        pr.join()
    assert list(tmp_path.glob("*.tmp*")) == []


_HOLD_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    os.environ["FLIGHT_DEALS_HOME"] = sys.argv[1]
    from flight_deals.state.store import flock_guard
    with flock_guard("brief"):
        sys.stdout.write("acquired\\n"); sys.stdout.flush()
        time.sleep(float(sys.argv[2]))
    """
)


def test_flock_second_holder_exits_1_already_running(tmp_path):
    """A real second process cannot take the lock the first holds — it exits 1
    with 'already running' on stderr (UPGRADE-PLAN §6, single-instance brief)."""
    env = dict(os.environ, FLIGHT_DEALS_HOME=str(tmp_path))
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT, str(tmp_path), "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    # Wait until the holder confirms it acquired the lock.
    assert holder.stdout.readline().strip() == "acquired"

    second = subprocess.run(
        [sys.executable, "-c", _HOLD_SCRIPT, str(tmp_path), "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    assert second.returncode == 1
    assert "already running" in second.stderr

    holder.terminate()
    holder.wait(timeout=5)


def test_flock_released_allows_next_holder(tmp_path):
    """Once the first holder exits, the lock is free for the next run."""
    env = dict(os.environ, FLIGHT_DEALS_HOME=str(tmp_path))
    first = subprocess.run(
        [sys.executable, "-c", _HOLD_SCRIPT, str(tmp_path), "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    assert first.returncode == 0 and "acquired" in first.stdout
    second = subprocess.run(
        [sys.executable, "-c", _HOLD_SCRIPT, str(tmp_path), "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    assert second.returncode == 0 and "acquired" in second.stdout
