"""History v2 (Task 7 req 4): observation-time windows, fixed badges, the
compare()/enrich standout-solid-baseline groups, and proof the legacy 10-column
CSV still loads unchanged."""

from datetime import datetime, timezone

from freezegun import freeze_time

from flight_deals.engine import combine
from flight_deals.history import PriceHistoryStore

# Legacy 10-column format, timestamp_utc first column (exactly what Task 1
# froze). If history v2 broke the loader, these rows would vanish or misparse.
LEGACY_CSV = (
    "timestamp_utc,origin,destination,departure_date,return_date,price,currency,source,connection_path,total_price\n"
    "2026-06-21T14:32:31.160896,BUD,CAG,2026-08-12,,76.53,EUR,ryanair,,76.53\n"
    "2026-06-21T14:32:31.222928,BUD,PMI,2026-08-15,,107.33,EUR,ryanair,,107.33\n"
)


def _store(tmp_path, extra_rows=""):
    p = tmp_path / "price_history.csv"
    p.write_text(LEGACY_CSV + extra_rows)
    return PriceHistoryStore(csv_path=str(p))


@freeze_time("2026-07-01T12:00:00+00:00")
def test_legacy_csv_rows_load_unchanged(tmp_path):
    store = _store(tmp_path)
    hist = store.get_history(origin="BUD", destination="CAG")
    assert len(hist) == 1
    row = hist[0]
    assert row.price == 76.53 and row.currency == "EUR" and row.source == "ryanair"
    assert row.departure_date == "2026-08-12"
    # both legacy rows survive a full load
    assert len(store._load_rows()) == 2


@freeze_time("2026-07-01T12:00:00+00:00")
def test_window_filters_by_observation_time_not_departure(tmp_path):
    # An observation seen 400 days ago (outside the 365d window) must be dropped
    # by OBSERVATION time even though its flight departs in the future.
    old_seen = "2025-05-01T00:00:00+00:00"
    rows = (
        f"{old_seen},BUD,CFU,2026-08-23,2026-08-29,999.0,EUR,ryanair,,999.0\n"
        "2026-06-25T00:00:00+00:00,BUD,CFU,2026-08-23,2026-08-29,120.0,EUR,ryanair,,120.0\n"
    )
    store = _store(tmp_path, rows)
    stats = store.get_route_stats("BUD", "CFU")
    assert stats["count"] == 1          # the 400-day-old observation is excluded
    assert stats["min_price"] == 120.0


@freeze_time("2026-07-01T12:00:00+00:00")
def test_best_this_month_is_min_over_last_30d_observations(tmp_path):
    rows = (
        # seen 45 days ago: outside the last-30d observation window
        "2026-05-17T00:00:00+00:00,BUD,CFU,2026-08-23,2026-08-29,60.0,EUR,ryanair,,60.0\n"
        # seen 10 & 5 days ago: inside it
        "2026-06-21T00:00:00+00:00,BUD,CFU,2026-08-23,2026-08-29,140.0,EUR,ryanair,,140.0\n"
        "2026-06-26T00:00:00+00:00,BUD,CFU,2026-08-23,2026-08-29,110.0,EUR,ryanair,,110.0\n"
    )
    store = _store(tmp_path, rows)
    stats = store.get_route_stats("BUD", "CFU")
    # overall min is 60 (seen 45d ago) but best_this_month compares to the
    # last-30d minimum (110), which the overall min (60) does NOT tie/beat.
    assert stats["min_price"] == 60.0
    assert stats["best_this_month"] is False


@freeze_time("2026-07-01T12:00:00+00:00")
def test_compare_and_enrich_groups(tmp_path):
    # 6 recent observations -> median 150; enough for the standout gate.
    prices = [200, 180, 160, 140, 120, 100]
    rows = "".join(
        f"2026-06-2{i}T00:00:00+00:00,BUD,CFU,2026-08-23,2026-08-29,{p}.0,EUR,ryanair,,{p}.0\n"
        for i, p in enumerate(prices)
    )
    store = _store(tmp_path, rows)

    cmp_standout = store.compare("BUD", "CFU", 100.0)
    assert cmp_standout["count"] == 6 and cmp_standout["sufficient"] is True
    assert cmp_standout["median"] == 150.0
    assert round(cmp_standout["pct_vs_typical"], 3) == 0.333

    deals = [
        {"origin": "BUD", "destination": "CFU", "price_eur": 100.0, "price_confidence": "exact", "return_date": "2026-08-29"},
        {"origin": "BUD", "destination": "CFU", "price_eur": 145.0, "price_confidence": "exact", "return_date": "2026-08-29"},
        {"origin": "BUD", "destination": "CFU", "price_eur": 200.0, "price_confidence": "exact", "return_date": "2026-08-29"},
    ]
    combine.enrich(deals, store)
    assert deals[0]["group"] == "standout"
    assert "33% below" in deals[0]["why"] and "6 observations" in deals[0]["why"]
    assert deals[1]["group"] == "solid"
    assert deals[2]["group"] == "baseline"


@freeze_time("2026-07-01T12:00:00+00:00")
def test_enrich_insufficient_history_is_honest(tmp_path):
    store = _store(tmp_path)  # CAG/PMI only, CFU has 0 observations
    deals = [{"origin": "BUD", "destination": "CFU", "price_eur": 99.0,
              "price_confidence": "exact", "return_date": "2026-08-29"}]
    combine.enrich(deals, store)
    assert deals[0]["group"] == "baseline"
    assert "insufficient history" in deals[0]["why"]
