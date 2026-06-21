import pytest
from flight_deals.history import PriceHistoryStore
from flight_deals.models import PriceSnapshot
from datetime import datetime, timezone
import tempfile
import os

def test_history_append_and_read():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        path = tmp.name

    # Write header first
    with open(path, "w") as f:
        f.write("timestamp_utc,origin,destination,departure_date,return_date,price,currency,source\n")

    store = PriceHistoryStore(csv_path=path)
    snapshot = PriceSnapshot(
        timestamp_utc=datetime.now(timezone.utc),
        origin="STN",
        destination="BGY",
        departure_date="2026-08-20",
        price=49.99,
        currency="GBP",
        source="ryanair",
    )
    store.append(snapshot)

    previous = store.get_previous_price("STN", "BGY", "2026-08-20")
    assert previous == 49.99

    os.unlink(path)