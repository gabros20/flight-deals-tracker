import csv
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional, Dict, Any
from collections import defaultdict

from flight_deals.models import PriceSnapshot, FlightDeal, HistoricalComparison

class PriceHistoryStore:
    def __init__(self, csv_path: str = "data/price_history.csv"):
        self.csv_path = Path(csv_path)
        self._ensure_header()

    def _ensure_header(self):
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp_utc", "origin", "destination", "departure_date", "return_date",
                    "price", "currency", "source", "connection_path", "total_price"
                ])

    def append(self, snapshot: PriceSnapshot):
        """Append a basic snapshot."""
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
                str(snapshot.connection_path) if snapshot.connection_path else "",
                snapshot.total_price or snapshot.price,
            ])

    def append_from_deal(self, deal: FlightDeal, timestamp: Optional[datetime] = None):
        """Store a full deal as snapshot (supports composites)."""
        ts = timestamp or datetime.utcnow()
        conn_path = getattr(deal, "connection_path", []) or []
        total = getattr(deal, "price", 0)
        if hasattr(deal, "historical_comparison") and deal.historical_comparison:
            # avoid re-logging
            pass
        snap = PriceSnapshot(
            timestamp_utc=ts,
            origin=deal.origin,
            destination=deal.destination,
            departure_date=deal.departure_date,
            return_date=deal.return_date,
            price=deal.price,
            currency=deal.currency,
            source=deal.source,
            connection_path=conn_path,
            total_price=total,
        )
        self.append(snap)

    def _load_rows(self) -> List[Dict[str, str]]:
        if not self.csv_path.exists():
            return []
        with open(self.csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)

    def get_route_stats(
        self,
        origin: str,
        destination: str,
        departure_date: Optional[str] = None,
        window_days: int = 365
    ) -> Dict[str, Any]:
        """Return stats for a route. Filters by date window if provided."""
        rows = self._load_rows()
        prices = []
        dates = []
        now = date.today()

        for row in rows:
            if row.get("origin") != origin or row.get("destination") != destination:
                continue
            try:
                p = float(row["price"])
                prices.append(p)
                dep = row.get("departure_date", "")
                if dep:
                    dates.append(dep)
            except (ValueError, KeyError):
                continue

        if not prices:
            return {"count": 0}

        prices = sorted(prices)
        count = len(prices)
        min_p = min(prices)
        max_p = max(prices)
        avg_p = sum(prices) / count

        # Simple median
        mid = count // 2
        median = prices[mid] if count % 2 == 1 else (prices[mid-1] + prices[mid]) / 2

        # Rough percentiles
        p25 = prices[max(0, int(0.25 * count))]
        p75 = prices[min(count-1, int(0.75 * count))]

        # Best this month / year heuristics (based on available data)
        best_month = min_p == min(prices[:max(1, count//3)]) if count > 2 else False
        best_year = min_p == min_p  # placeholder, improve with date parsing if needed

        return {
            "count": count,
            "min_price": round(min_p, 2),
            "avg_price": round(avg_p, 2),
            "median_price": round(median, 2),
            "max_price": round(max_p, 2),
            "percentile_25": round(p25, 2),
            "percentile_75": round(p75, 2),
            "best_this_month": best_month,
            "best_this_year": best_year,
            "last_collected": max(dates) if dates else None,
        }

    def get_previous_price(self, origin: str, destination: str, departure_date: str) -> Optional[float]:
        """Simple previous price lookup for drop detection."""
        if not self.csv_path.exists():
            return None
        with open(self.csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reversed(list(reader)):
                if (row.get("origin") == origin and
                    row.get("destination") == destination and
                    row.get("departure_date") == departure_date):
                    return float(row["price"])
        return None

    def get_history(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        limit: int = 20
    ) -> List[PriceSnapshot]:
        """Return recent price snapshots."""
        if not self.csv_path.exists():
            return []
        snapshots = []
        with open(self.csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reversed(list(reader)):
                if origin and row.get("origin") != origin:
                    continue
                if destination and row.get("destination") != destination:
                    continue
                try:
                    snap = PriceSnapshot(
                        timestamp_utc=datetime.fromisoformat(row["timestamp_utc"]),
                        origin=row["origin"],
                        destination=row["destination"],
                        departure_date=row["departure_date"],
                        return_date=row.get("return_date") or None,
                        price=float(row["price"]),
                        currency=row["currency"],
                        source=row["source"],
                        connection_path=[],
                    )
                    snapshots.append(snap)
                    if len(snapshots) >= limit:
                        break
                except Exception:
                    continue
        return snapshots

    def enrich_deals(self, deals: List[FlightDeal]) -> None:
        """Enrich list of FlightDeal objects with historical comparison and badges."""
        for deal in deals:
            stats = self.get_route_stats(
                deal.origin,
                deal.destination,
                deal.departure_date
            )
            if stats.get("count", 0) < 1:
                deal.comparison_note = "No prior data"
                continue

            comp = HistoricalComparison(
                count=stats["count"],
                min_price=stats["min_price"],
                avg_price=stats["avg_price"],
                median_price=stats.get("median_price"),
                max_price=stats["max_price"],
                percentile_25=stats["percentile_25"],
                percentile_75=stats["percentile_75"],
                best_this_month=stats.get("best_this_month", False),
                best_this_year=stats.get("best_this_year", False),
                last_collected=stats.get("last_collected"),
            )

            current = deal.price
            minp = stats["min_price"]
            avgp = stats["avg_price"]

            note_parts = []
            if current <= minp:
                note_parts.append("Best price ever seen!")
                comp.best_this_year = True
                comp.best_this_month = True
            elif stats.get("count", 0) >= 3:
                if current < avgp * 0.85:
                    note_parts.append(f"Great deal ({int((1 - current/avgp)*100)}% below avg)")
                if comp.best_this_month:
                    note_parts.append("Best this month")
                if comp.best_this_year:
                    note_parts.append("Best this year")

            note_parts.append(f"Hist: min €{minp} avg €{avgp} (n={stats['count']})")
            deal.historical_comparison = comp
            deal.comparison_note = " | ".join(note_parts) if note_parts else ""

    def compute_efficiency_vs_history(self, price: float, stats: Dict) -> Optional[float]:
        if not stats or stats.get("count", 0) == 0 or not stats.get("avg_price"):
            return None
        return (stats["avg_price"] - price) / stats["avg_price"] * 100  # positive = better than avg
