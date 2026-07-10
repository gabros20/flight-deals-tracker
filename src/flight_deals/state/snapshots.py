"""Append-only deal observations (UPGRADE-PLAN §4 "Deal identity & check").

Every displayed deal is snapshotted: one JSONL file per ``deal_id`` under
``data/deals/``, each line an observation ``{deal_id, seen_at, price_eur,
price_confidence, ...}``. ``check <deal_id>`` re-queries the live exact price
and reports the delta vs the *latest* and the *first* observation. Because the
``deal_id`` excludes price (CONTRACT §5), the same trip re-priced tomorrow
lands in the same file — that's what makes a delta meaningful.

Writes go through ``state.store.append_jsonl`` (atomic single-line append), so
concurrent snapshots from a parallel sweep never interleave a partial record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flight_deals.paths import resolve_path
from flight_deals.state import store

SCHEMA_VERSION = 1
DEALS_SUBDIR = "data/deals"


def _deals_dir() -> Path:
    return resolve_path(DEALS_SUBDIR)


def path_for(deal_id: str) -> Path:
    return _deals_dir() / f"{deal_id}.jsonl"


def _record_from_deal(deal: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "deal_id": deal["deal_id"],
        "seen_at": now.isoformat(),
        "price_eur": deal["price_eur"],
        "price_confidence": deal["price_confidence"],
        "origin": deal["origin"],
        "destination": deal["destination"],
        "out_date": deal["out_date"],
        "return_date": deal.get("return_date"),
        "shape": deal["shape"],
        "carriers": deal["carriers"],
    }


def snapshot(deal: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Append one observation for a rendered Deal dict; return the record."""
    now = now or datetime.now(timezone.utc)
    record = _record_from_deal(deal, now)
    store.append_jsonl(path_for(deal["deal_id"]), record)
    return record


def records(deal_id: str) -> List[Dict[str, Any]]:
    """All observations for a deal, oldest first."""
    return store.read_jsonl(path_for(deal_id))


def latest(deal_id: str) -> Optional[Dict[str, Any]]:
    recs = records(deal_id)
    return recs[-1] if recs else None


def first(deal_id: str) -> Optional[Dict[str, Any]]:
    recs = records(deal_id)
    return recs[0] if recs else None
