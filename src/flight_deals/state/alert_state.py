"""The alert state machine (UPGRADE-PLAN §4 "Alert state model").

This is the load-bearing correctness core of the monitoring loop: it decides
whether a confirmed price crossing a watch's threshold should actually *fire*
a Telegram alert, or be swallowed as a duplicate. Without it, an hourly
``brief`` would re-alert the same drop every run.

State is ``data/alert_state.json``, keyed ``(search_name, route, month)``::

    { "august-seaside|BUD-CFU|2026-08":
        { "last_alert_price": 138.0, "last_alert_at": "...", "expires_at": "...",
          "state": "alerted" } }

States: ``new → alerted → suppressed → re-armed``.

* **new** (no entry): the first confirmed price at/under ``max_price`` **fires**.
* **alerted / suppressed**: we already alerted at ``last_alert_price``. A newer
  confirmed price re-alerts **only** if it is at least ``realert_drop_pct``
  (default 15%, chosen above Wizz's ±10% noise) below ``last_alert_price``.
  A price merely still-in-band, or rising back, does nothing.
* **re-armed**: once ``now`` passes ``expires_at`` (the watched month has ended
  — a fare within it can no longer be booked), the entry is forgotten and the
  next crossing fires fresh.

INVARIANT (test-pinned, load-bearing): only a ``price_confidence == "exact"``
deal can ever fire. Approximate (Wizz timetable) prices are estimates and are
double-guarded here even though the estimate→confirm pipeline already refuses
to promote them — belt and braces on the one rule that must never break.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from flight_deals.paths import resolve_path
from flight_deals.state import store

SCHEMA_VERSION = 1
STATE_SUBPATH = "data/alert_state.json"
DEFAULT_REALERT_DROP_PCT = 15.0


def route_of(deal: Dict[str, Any]) -> str:
    return f"{deal['origin']}-{deal['destination']}"


def month_of(deal: Dict[str, Any]) -> str:
    """The watched month a deal belongs to = its outbound calendar month."""
    return str(deal["out_date"])[:7]


def _month_end(month: str) -> datetime:
    """Last instant of a ``YYYY-MM`` month, UTC. After this the fares within it
    can no longer be booked, so a watch keyed on it re-arms."""
    year, mon = (int(x) for x in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return datetime(year, mon, last_day, 23, 59, 59, tzinfo=timezone.utc)


def _key(search_name: str, route: str, month: str) -> str:
    return f"{search_name}|{route}|{month}"


class AlertMachine:
    """Loads the alert state, evaluates crossings, and persists on ``save()``.
    Deliberately keeps the whole (small) map in memory across a ``brief`` run so
    several searches share one atomic write at the end."""

    def __init__(self, path: Optional[Path] = None, *, realert_drop_pct: float = DEFAULT_REALERT_DROP_PCT):
        self.path = Path(path) if path else resolve_path(STATE_SUBPATH)
        self.realert_drop_pct = float(realert_drop_pct)
        data = store.read_versioned(self.path, current=SCHEMA_VERSION) or {}
        self._entries: Dict[str, Dict[str, Any]] = dict(data.get("entries", {}))

    # -- introspection (used by tests / brief digests) --------------------- #
    def get(self, search_name: str, route: str, month: str) -> Optional[Dict[str, Any]]:
        return self._entries.get(_key(search_name, route, month))

    def evaluate(
        self,
        *,
        search_name: str,
        deal: Dict[str, Any],
        max_price: float,
        now: Optional[datetime] = None,
    ) -> bool:
        """Decide whether ``deal`` fires an alert for ``search_name`` and update
        the in-memory state accordingly. Returns ``True`` iff an alert should be
        sent now. Call :meth:`save` once after evaluating a whole brief run.

        Only confirmed-exact prices can fire (the load-bearing invariant); an
        approximate deal returns ``False`` and never mutates state."""
        now = (now or datetime.now(timezone.utc))
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)

        # INVARIANT: approximate prices never alert (double-guard).
        if deal.get("price_confidence") != "exact":
            return False

        price = float(deal["price_eur"])
        route = route_of(deal)
        month = month_of(deal)
        key = _key(search_name, route, month)
        expires_at = _month_end(month)

        entry = self._entries.get(key)

        # Re-arm: an entry whose watched month has ended is forgotten, so the
        # next crossing fires fresh (state new → ... → re-armed → alerted).
        if entry is not None and now >= datetime.fromisoformat(entry["expires_at"]):
            self._entries.pop(key, None)
            entry = None

        crossing = price <= max_price

        if entry is None:
            if crossing:
                self._fire(key, price, now, expires_at)
                return True
            return False

        # Already alerted at last_alert_price. Re-alert only on a further drop
        # of >= realert_drop_pct below it (rising back / still-in-band = silent).
        last = float(entry["last_alert_price"])
        threshold = last * (1.0 - self.realert_drop_pct / 100.0)
        if crossing and price <= threshold:
            self._fire(key, price, now, expires_at)
            return True

        # Not a re-alert: settle into the suppressed state (no message).
        entry["state"] = "suppressed"
        return False

    def _fire(self, key: str, price: float, now: datetime, expires_at: datetime) -> None:
        self._entries[key] = {
            "last_alert_price": round(price, 2),
            "last_alert_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "state": "alerted",
        }

    def prune_expired(self, now: Optional[datetime] = None) -> int:
        """Drop entries whose watched month has ended. Returns the count removed
        (brief calls this on its prune pass so the state file stays bounded)."""
        now = (now or datetime.now(timezone.utc))
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        stale = [k for k, e in self._entries.items() if now >= datetime.fromisoformat(e["expires_at"])]
        for k in stale:
            self._entries.pop(k, None)
        return len(stale)

    def save(self) -> None:
        store.atomic_write_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, "entries": self._entries},
        )
