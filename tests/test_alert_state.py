"""The alert state machine (UPGRADE-PLAN §4): new→alerted→suppressed→re-armed.

The four load-bearing behaviours, all under a frozen clock:
  * brief-twice-one-alert (a suppressed drop never re-fires);
  * a >=15% further drop re-alerts;
  * a rise-back does nothing;
  * expiry (watched month ends) re-arms;
  * approximate prices never alert (the invariant, double-guarded here).
"""

from datetime import datetime, timezone

from freezegun import freeze_time

from flight_deals.state.alert_state import AlertMachine


def _deal(price, *, confidence="exact", out_date="2026-08-20", dest="CFU"):
    return {
        "deal_id": f"{dest}-{price}", "shape": "S2", "origin": "BUD", "destination": dest,
        "out_date": out_date, "return_date": "2026-08-25",
        "price_eur": float(price), "price_confidence": confidence, "carriers": ["ryanair"],
    }


def _machine(tmp_path, **kw):
    return AlertMachine(path=tmp_path / "alert_state.json", **kw)


def test_first_confirmed_crossing_fires(tmp_path):
    m = _machine(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=now) is True
    entry = m.get("w", "BUD-CFU", "2026-08")
    assert entry["state"] == "alerted" and entry["last_alert_price"] == 140.0


def test_above_threshold_does_not_fire(tmp_path):
    m = _machine(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert m.evaluate(search_name="w", deal=_deal(160), max_price=150, now=now) is False
    assert m.get("w", "BUD-CFU", "2026-08") is None


def test_brief_twice_one_alert(tmp_path):
    """Two brief runs at the same in-band price -> exactly one alert."""
    m = _machine(tmp_path)
    with freeze_time("2026-07-01T08:30:00+00:00"):
        first = m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=datetime.now(timezone.utc))
    with freeze_time("2026-07-01T13:30:00+00:00"):
        second = m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=datetime.now(timezone.utc))
    assert (first, second) == (True, False)
    assert m.get("w", "BUD-CFU", "2026-08")["state"] == "suppressed"


def test_further_15pct_drop_re_alerts(tmp_path):
    m = _machine(tmp_path, realert_drop_pct=15.0)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=now) is True
    # 140 * 0.85 = 119 -> 120 is NOT enough, 118 is.
    assert m.evaluate(search_name="w", deal=_deal(120), max_price=150, now=now) is False
    assert m.evaluate(search_name="w", deal=_deal(118), max_price=150, now=now) is True
    assert m.get("w", "BUD-CFU", "2026-08")["last_alert_price"] == 118.0


def test_rise_back_into_band_does_nothing(tmp_path):
    m = _machine(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert m.evaluate(search_name="w", deal=_deal(120), max_price=150, now=now) is True
    # price rises back but still under threshold -> no new alert
    assert m.evaluate(search_name="w", deal=_deal(145), max_price=150, now=now) is False
    # and the recorded alert price is unchanged
    assert m.get("w", "BUD-CFU", "2026-08")["last_alert_price"] == 120.0


def test_expiry_re_arms_same_key_and_refires(tmp_path):
    """Same (search, route, month) key throughout: a confirmed crossing fires,
    the clock advances past the watched month's ``expires_at``, and a new
    confirmed crossing on that SAME key must alert again (re-armed), not stay
    permanently suppressed. This exercises the in-``evaluate`` re-arm branch
    (an entry whose month has ended is popped before the crossing check) —
    unlike a two-different-months test, this one fails if that branch is
    deleted (see below)."""
    m = _machine(tmp_path)
    with freeze_time("2026-07-01T08:30:00+00:00"):
        assert m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=datetime.now(timezone.utc)) is True
    assert m.get("w", "BUD-CFU", "2026-08")["state"] == "alerted"

    # After the watched month (2026-08) ends, the SAME key's next confirmed
    # crossing (same price, still in-band) must fire again.
    with freeze_time("2026-09-05T08:30:00+00:00"):
        assert m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=datetime.now(timezone.utc)) is True
    entry = m.get("w", "BUD-CFU", "2026-08")
    assert entry["state"] == "alerted" and entry["last_alert_price"] == 140.0


def test_approximate_never_alerts(tmp_path):
    m = _machine(tmp_path)
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # Well under threshold, but approximate confidence -> never fires, no state.
    assert m.evaluate(search_name="w", deal=_deal(50, confidence="approximate"), max_price=150, now=now) is False
    assert m.get("w", "BUD-CFU", "2026-08") is None


def test_state_persists_across_instances(tmp_path):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    m1 = _machine(tmp_path)
    assert m1.evaluate(search_name="w", deal=_deal(140), max_price=150, now=now) is True
    m1.save()
    # A fresh machine (next brief run) must not re-alert the same drop.
    m2 = _machine(tmp_path)
    assert m2.evaluate(search_name="w", deal=_deal(140), max_price=150, now=now) is False


def test_prune_expired_removes_past_month_entries(tmp_path):
    m = _machine(tmp_path)
    with freeze_time("2026-07-01T08:30:00+00:00"):
        m.evaluate(search_name="w", deal=_deal(140), max_price=150, now=datetime.now(timezone.utc))
    removed = m.prune_expired(datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert removed == 1
    assert m.get("w", "BUD-CFU", "2026-08") is None
