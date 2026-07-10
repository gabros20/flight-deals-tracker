import csv
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

from flight_deals.models import PriceSnapshot, FlightDeal, HistoricalComparison
from flight_deals.config import get_config, FlightDealsConfig

logger = logging.getLogger(__name__)


class PriceHistoryStore:
    def __init__(self, csv_path: str = None, config: Optional[FlightDealsConfig] = None):
        self.config = config or get_config()
        if csv_path:
            self.csv_path = Path(csv_path)
        else:
            self.csv_path = self.config.history_path
        self.alerts_path = self.config.alerts_path
        self.window_days = getattr(self.config, "history_window_days", 365)
        self.drop_threshold = getattr(self.config, "price_drop_threshold", 0.15)
        self.min_points = getattr(self.config, "history_min_points_for_badge", 3)
        self._cached_rows: Optional[List[Dict[str, str]]] = None
        self._ensure_header()
        self._ensure_alerts_header()

    def _ensure_header(self):
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp_utc", "origin", "destination", "departure_date", "return_date",
                    "price", "currency", "source", "connection_path", "total_price"
                ])

    def _ensure_alerts_header(self):
        if not self.alerts_path.exists():
            self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.alerts_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp_utc", "origin", "destination", "departure_date", "current_price",
                    "historical_avg", "pct_below_avg", "threshold", "message"
                ])

    def _load_rows(self, force_reload: bool = False) -> List[Dict[str, str]]:
        """Optimized file-based load with simple in-memory cache."""
        if self._cached_rows is not None and not force_reload:
            return self._cached_rows
        if not self.csv_path.exists():
            self._cached_rows = []
            return []
        with open(self.csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            self._cached_rows = list(reader)
        return self._cached_rows

    def _parse_date(self, dstr: str) -> Optional[date]:
        if not dstr:
            return None
        try:
            return datetime.fromisoformat(dstr[:10]).date() if "T" in dstr or "-" in dstr else date.fromisoformat(dstr)
        except (ValueError, TypeError):
            try:
                return date.fromisoformat(dstr.split("T")[0])
            except (ValueError, TypeError):
                return None

    def _filter_by_window(self, rows: List[Dict], window_days: Optional[int] = None) -> List[Dict]:
        """Robust date-window filtering for file-based data."""
        if window_days is None:
            window_days = self.window_days
        if window_days <= 0:
            return rows
        cutoff = date.today() - timedelta(days=window_days)
        filtered = []
        for row in rows:
            dep = self._parse_date(row.get("departure_date", ""))
            if dep is None or dep >= cutoff:
                filtered.append(row)
        return filtered

    def append(self, snapshot: PriceSnapshot):
        """Append a basic snapshot (file-based)."""
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
        self._cached_rows = None  # invalidate cache

    def append_from_deal(self, deal: FlightDeal, timestamp: Optional[datetime] = None):
        """Store a full deal as snapshot (supports composites)."""
        ts = timestamp or datetime.now(timezone.utc)
        conn_path = getattr(deal, "connection_path", []) or []
        total = getattr(deal, "price", 0)
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

    def get_route_stats(
        self,
        origin: str,
        destination: str,
        departure_date: Optional[str] = None,
        window_days: Optional[int] = None
    ) -> Dict[str, Any]:
        """Return stats for a route. Applies robust date window filtering."""
        rows = self._load_rows()
        rows = self._filter_by_window(rows, window_days)
        prices = []
        dates = []
        recent_prices = []  # for best this month logic

        cutoff_month = date.today() - timedelta(days=30)

        for row in rows:
            if row.get("origin") != origin or row.get("destination") != destination:
                continue
            try:
                p = float(row["price"])
                dep = self._parse_date(row.get("departure_date", ""))
                prices.append(p)
                if dep:
                    dates.append(str(dep))
                    if dep >= cutoff_month:
                        recent_prices.append(p)
            except (ValueError, KeyError):
                continue

        if not prices:
            return {"count": 0}

        prices = sorted(prices)
        count = len(prices)
        min_p = min(prices)
        max_p = max(prices)
        avg_p = sum(prices) / count

        mid = count // 2
        median = prices[mid] if count % 2 == 1 else (prices[mid-1] + prices[mid]) / 2

        p25 = prices[max(0, int(0.25 * count))]
        p75 = prices[min(count-1, int(0.75 * count))]

        # Robust best this month / year using actual dates
        best_this_month = False
        if recent_prices:
            best_this_month = min_p <= min(recent_prices)

        best_this_year = min_p == min(prices)  # conservative; could refine with year filter

        last_collected = max(dates) if dates else None

        return {
            "count": count,
            "min_price": round(min_p, 2),
            "avg_price": round(avg_p, 2),
            "median_price": round(median, 2),
            "max_price": round(max_p, 2),
            "percentile_25": round(p25, 2),
            "percentile_75": round(p75, 2),
            "best_this_month": best_this_month,
            "best_this_year": best_this_year,
            "last_collected": last_collected,
            "window_days_used": window_days or self.window_days,
        }

    def get_previous_price(self, origin: str, destination: str, departure_date: str) -> Optional[float]:
        """Simple previous price lookup."""
        rows = self._load_rows()
        for row in reversed(rows):
            if (row.get("origin") == origin and
                row.get("destination") == destination and
                row.get("departure_date", "").startswith(departure_date)):
                return float(row["price"])
        return None

    def get_history(
        self,
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        limit: int = 20,
        window_days: Optional[int] = None
    ) -> List[PriceSnapshot]:
        """Return recent price snapshots, optionally window filtered."""
        rows = self._filter_by_window(self._load_rows(), window_days)
        snapshots = []
        for row in reversed(rows):
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
            except (ValueError, KeyError) as e:
                logger.warning("history: skipping malformed row: %s", e)
                continue
        return snapshots

    def enrich_deals(self, deals: List[FlightDeal], window_days: Optional[int] = None) -> None:
        """Enrich list of FlightDeal objects with historical comparison and badges using window."""
        for deal in deals:
            stats = self.get_route_stats(
                deal.origin,
                deal.destination,
                deal.departure_date,
                window_days=window_days
            )
            if stats.get("count", 0) < self.min_points:
                deal.comparison_note = "No prior data" if stats.get("count", 0) == 0 else f"Insufficient history ({stats['count']} pts)"
                deal.historical_comparison = HistoricalComparison(count=stats.get("count", 0))
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
            pct_below = None
            if avgp > 0:
                pct_below = (avgp - current) / avgp

            if current <= minp:
                note_parts.append("Best price ever seen!")
                comp.best_this_year = True
                comp.best_this_month = True
            elif stats.get("count", 0) >= self.min_points:
                if pct_below is not None and pct_below >= self.drop_threshold:
                    note_parts.append(f"Great deal ({int(pct_below*100)}% below avg)")
                if comp.best_this_month:
                    note_parts.append("Best this month")
                if comp.best_this_year:
                    note_parts.append("Best this year")

            note_parts.append(f"Hist: min €{minp} avg €{avgp} (n={stats['count']})")
            deal.historical_comparison = comp
            deal.comparison_note = " | ".join(note_parts) if note_parts else ""

    def detect_price_drops(
        self,
        current_deals: List[FlightDeal],
        threshold: Optional[float] = None,
        window_days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Detect prices significantly below historical avg. Returns list of alert dicts. File-based logging."""
        if threshold is None:
            threshold = self.drop_threshold
        alerts = []
        for deal in current_deals:
            stats = self.get_route_stats(deal.origin, deal.destination, deal.departure_date, window_days)
            count = stats.get("count", 0)
            if count < self.min_points:
                continue
            avgp = stats.get("avg_price", 0)
            if avgp <= 0:
                continue
            current = deal.price
            pct_below = (avgp - current) / avgp
            if pct_below >= threshold:
                alert = {
                    "origin": deal.origin,
                    "destination": deal.destination,
                    "departure_date": deal.departure_date,
                    "current_price": current,
                    "historical_avg": avgp,
                    "pct_below_avg": round(pct_below * 100, 1),
                    "threshold": threshold,
                    "message": f"Great deal! {deal.origin}-{deal.destination} on {deal.departure_date} at €{current} is {int(pct_below*100)}% below historical avg (€{avgp})"
                }
                alerts.append(alert)
                self._log_alert(alert)
        return alerts

    def _log_alert(self, alert: Dict[str, Any]):
        """Append alert to file-based alerts log (git friendly CSV)."""
        ts = datetime.now(timezone.utc).isoformat()
        with open(self.alerts_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                ts,
                alert["origin"],
                alert["destination"],
                alert["departure_date"],
                alert["current_price"],
                alert["historical_avg"],
                alert["pct_below_avg"],
                alert["threshold"],
                alert["message"]
            ])

    def compute_efficiency_vs_history(self, price: float, stats: Dict) -> Optional[float]:
        if not stats or stats.get("count", 0) == 0 or not stats.get("avg_price"):
            return None
        return (stats["avg_price"] - price) / stats["avg_price"] * 100  # positive = better than avg

    def clear_cache(self):
        self._cached_rows = None
