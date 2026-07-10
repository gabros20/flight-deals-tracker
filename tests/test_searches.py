"""Saved searches (Task 8 req 2): CRUD, idempotent add, validation, and the
schedule `due` computation under a frozen clock.
"""

from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

from flight_deals.state import searches


@pytest.fixture(autouse=True)
def _isolated_searches_dir(tmp_path, monkeypatch):
    d = tmp_path / "searches"
    d.mkdir()
    monkeypatch.setattr(searches, "searches_dir", lambda: d)
    return d


def _spec(**over):
    base = {"origins": ["BUD"], "where": "seaside", "depart": "2026-08", "nights": "5-8"}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# CRUD                                                                         #
# --------------------------------------------------------------------------- #
def test_add_list_show_remove_roundtrip(_isolated_searches_dir):
    rec = searches.add(name="august-seaside", spec=_spec(), schedule="daily 08:30")
    assert rec["name"] == "august-seaside"
    assert [r["name"] for r in searches.list_all()] == ["august-seaside"]
    assert searches.load("august-seaside")["spec"]["where"] == "seaside"
    assert searches.remove("august-seaside") is True
    assert searches.list_all() == []
    assert searches.remove("august-seaside") is False


def test_add_is_idempotent_update(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(budget=100))
    searches.add(name="w", spec=_spec(budget=150))
    recs = searches.list_all()
    assert len(recs) == 1 and recs[0]["spec"]["budget"] == 150


def test_add_validates_spec(_isolated_searches_dir):
    with pytest.raises(searches.SearchError):
        searches.add(name="bad", spec={"origins": ["BUD"]})  # no depart


def test_add_validates_schedule_and_alert(_isolated_searches_dir):
    with pytest.raises(searches.SearchError):
        searches.add(name="bad", spec=_spec(), schedule="hourly 5")
    with pytest.raises(searches.SearchError):
        searches.add(name="bad", spec=_spec(), alert={"notify": "telegram"})  # no max_price


def test_watch_detection_and_name_slug(_isolated_searches_dir):
    rec = searches.add(name="BUD to CFU!", spec=_spec(), alert={"max_price": 150, "notify": "telegram"})
    assert rec["name"] == "bud-to-cfu"
    assert searches.is_watch(rec) is True
    assert searches.is_watch(searches.add(name="plain", spec=_spec())) is False


# --------------------------------------------------------------------------- #
# schedules / due                                                              #
# --------------------------------------------------------------------------- #
def test_parse_schedule_forms():
    assert searches.parse_schedule("daily 08:30").kind == "daily"
    assert searches.parse_schedule("weekly mon 08:30").weekday == 0
    assert searches.parse_schedule("every 6h").hours == 6.0
    for bad in ["", "daily", "weekly xyz 08:30", "daily 25:00", "every 0h"]:
        with pytest.raises(searches.SearchError):
            searches.parse_schedule(bad)


def test_daily_due_never_run_is_due(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(), schedule="daily 08:30")
    with freeze_time("2026-07-01T09:00:00+00:00"):
        assert [r["name"] for r in searches.due(datetime.now(timezone.utc))] == ["w"]


def test_daily_due_respects_last_run(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(), schedule="daily 08:30")
    # Ran at 08:45 today; a 09:00 brief is NOT due again (slot already consumed).
    searches.stamp_run("w", datetime(2026, 7, 1, 8, 45, tzinfo=timezone.utc))
    with freeze_time("2026-07-01T09:00:00+00:00"):
        assert searches.due(datetime.now(timezone.utc)) == []
    # Next day past the slot -> due again.
    with freeze_time("2026-07-02T09:00:00+00:00"):
        assert [r["name"] for r in searches.due(datetime.now(timezone.utc))] == ["w"]


def test_daily_before_slot_not_due(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(), schedule="daily 08:30")
    searches.stamp_run("w", datetime(2026, 6, 30, 8, 45, tzinfo=timezone.utc))
    # 07:00 today is before today's 08:30 slot; last run was yesterday's slot.
    with freeze_time("2026-07-01T07:00:00+00:00"):
        assert searches.due(datetime.now(timezone.utc)) == []


def test_every_nh_interval(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(), schedule="every 6h")
    searches.stamp_run("w", datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc))
    with freeze_time("2026-07-01T11:00:00+00:00"):  # only 5h later
        assert searches.due(datetime.now(timezone.utc)) == []
    with freeze_time("2026-07-01T12:30:00+00:00"):  # 6.5h later
        assert [r["name"] for r in searches.due(datetime.now(timezone.utc))] == ["w"]


def test_weekly_due(_isolated_searches_dir):
    searches.add(name="w", spec=_spec(), schedule="weekly mon 08:30")
    # 2026-07-06 is a Monday.
    searches.stamp_run("w", datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc))
    with freeze_time("2026-07-08T09:00:00+00:00"):  # Wednesday, same week
        assert searches.due(datetime.now(timezone.utc)) == []
    with freeze_time("2026-07-13T09:00:00+00:00"):  # next Monday past slot
        assert [r["name"] for r in searches.due(datetime.now(timezone.utc))] == ["w"]


def test_force_all_ignores_schedule(_isolated_searches_dir):
    searches.add(name="scheduled", spec=_spec(), schedule="daily 08:30")
    searches.add(name="unscheduled", spec=_spec())
    # Consume yesterday's slot so "scheduled" isn't due at 07:00 today, and an
    # unscheduled search is never automatically due.
    searches.stamp_run("scheduled", datetime(2026, 6, 30, 8, 45, tzinfo=timezone.utc))
    with freeze_time("2026-07-01T07:00:00+00:00"):
        assert searches.due(datetime.now(timezone.utc)) == []  # before slot, none due
        forced = {r["name"] for r in searches.due(datetime.now(timezone.utc), force_all=True)}
    assert forced == {"scheduled", "unscheduled"}


def test_prune_stale_runs(_isolated_searches_dir):
    searches.add(name="keep", spec=_spec())
    searches.stamp_run("keep", datetime(2026, 7, 1, tzinfo=timezone.utc))
    searches.stamp_run("ghost", datetime(2026, 7, 1, tzinfo=timezone.utc))  # no file
    assert searches.prune_stale_runs() == ["ghost"]
    assert searches.last_run_at("keep") is not None
    assert searches.last_run_at("ghost") is None
