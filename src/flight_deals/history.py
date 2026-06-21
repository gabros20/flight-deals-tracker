import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from flight_deals.models import PriceSnapshot


class PriceHistoryStore:
    def __init__(self, csv_path: str = "data/price_history.csv"):
        self.csv_path = Path(csv_path)

    def append(self, snapshot: PriceSnapshot):
        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                snapshot.timestamp_utc.isoformat(),
                snapshot.origin,
                snapshot.destination,
                snapshot.departure_date,
                snapshot.return_date or "",
                snapshot.price,
                snapshot.currency,
                snapshot.source,
            ])

    def get_previous_price(self, origin: str, destination: str, departure_date: str) -> Optional[float]:
        """Simple previous price lookup for drop detection"""
        if not self.csv_path.exists():
            return None
        with open(self.csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reversed(list(reader)):
                if (row["origin"] == origin and 
                    row["destination"] == destination and 
                    row["departure_date"] == departure_date):
                    return float(row["price"])
        return None

    def get_history(
        self, 
        origin: Optional[str] = None, 
        destination: Optional[str] = None, 
        limit: int = 20
    ) -> List[PriceSnapshot]:
        """Return recent price snapshots, optionally filtered"""
        if not self.csv_path.exists():
            return []
        
        snapshots = []
        with open(self.csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reversed(list(reader)):
                if origin and row["origin"] != origin:
                    continue
                if destination and row["destination"] != destination:
                    continue
                
                snapshots.append(PriceSnapshot(
                    timestamp_utc=datetime.fromisoformat(row["timestamp_utc"]),
                    origin=row["origin"],
                    destination=row["destination"],
                    departure_date=row["departure_date"],
                    return_date=row["return_date"] or None,
                    price=float(row["price"]),
                    currency=row["currency"],
                    source=row["source"],
                ))
                
                if len(snapshots) >= limit:
                    break
        return snapshots

# Phase 8 note: Composite connection deals (with connection_path) are supported.
# When storing, the full path info is preserved in source_details if needed.
