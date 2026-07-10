"""state/store.py atomic helpers + state/snapshots.py append-only observations,
including a freezegun round-trip proving seen_at is the frozen clock."""

from datetime import datetime, timezone

from freezegun import freeze_time

from flight_deals.state import snapshots, store


def _deal(price):
    return {"deal_id": "abc1234567", "shape": "S2", "origin": "BUD", "destination": "CFU",
            "out_date": "2026-08-23", "return_date": "2026-08-29",
            "price_eur": price, "price_confidence": "exact", "carriers": ["ryanair"]}


def test_atomic_write_json_injects_schema_version(tmp_path):
    p = tmp_path / "state.json"
    store.atomic_write_json(p, {"a": 1})
    import json
    data = json.loads(p.read_text())
    assert data == {"a": 1, "schema_version": 1}
    # no leftover tmp files
    assert list(tmp_path.glob("*.tmp*")) == []


def test_append_jsonl_and_read(tmp_path):
    p = tmp_path / "obs.jsonl"
    store.append_jsonl(p, {"x": 1})
    store.append_jsonl(p, {"x": 2})
    assert store.read_jsonl(p) == [{"x": 1}, {"x": 2}]
    assert store.read_jsonl(tmp_path / "missing.jsonl") == []


def test_snapshot_append_latest_first_under_freezegun(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_deals_dir", lambda: tmp_path)

    with freeze_time("2026-06-01T09:00:00+00:00"):
        rec1 = snapshots.snapshot(_deal(100.0))
    with freeze_time("2026-06-05T09:00:00+00:00"):
        snapshots.snapshot(_deal(85.0))

    assert rec1["seen_at"] == "2026-06-01T09:00:00+00:00"
    assert rec1["price_eur"] == 100.0
    assert snapshots.first("abc1234567")["price_eur"] == 100.0
    assert snapshots.latest("abc1234567")["price_eur"] == 85.0
    assert len(snapshots.records("abc1234567")) == 2
    assert snapshots.latest("nope") is None


def test_snapshot_explicit_now_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "_deals_dir", lambda: tmp_path)
    when = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rec = snapshots.snapshot(_deal(50.0), now=when)
    assert rec["seen_at"] == when.isoformat()
