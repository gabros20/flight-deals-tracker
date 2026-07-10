"""Append-only deal observations (UPGRADE-PLAN §4 "Deal identity & check").

Every displayed deal is snapshotted: one JSONL file per ``deal_id`` under
``data/deals/``, each line an observation ``{deal_id, seen_at, price_eur,
price_confidence, ...}``. ``check <deal_id>`` re-queries the live exact price
and reports the delta vs the *latest* and the *first* observation. Because the
``deal_id`` excludes price (CONTRACT §5), the same trip re-priced tomorrow
lands in the same file — that's what makes a delta meaningful.

Writes go through ``state.store.append_jsonl`` (atomic single-line append), so
concurrent snapshots from a parallel sweep never interleave a partial record.

Same-day dedup: a display re-running the same search minutes later would
otherwise append an identical observation every time; ``snapshot()`` skips the
append when the latest existing record for the deal already has the same
``price_eur`` *and* the same UTC calendar day — a genuine price change (or the
next day's first sighting) still gets its own line.

Two-store split (Task 7): this JSONL store is the **authoritative source for
deal identity and check-time deltas** — one file per ``deal_id``, fed by every
displayed deal (``getaway``/``oneway``/``check``). The CSV history in
``history.py`` (fed by the cron ``run``/``track`` path) is the authoritative
source for price-context/typical-price stats instead; ``getaway`` *reads* that
CSV for its "why" context but never writes to it. The two stores are
deliberately not merged — different write cadences, different readers.
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


def _same_utc_day(prior_seen_at: str, now: datetime) -> bool:
    prior_dt = datetime.fromisoformat(prior_seen_at)
    if prior_dt.tzinfo is None:
        prior_dt = prior_dt.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return prior_dt.astimezone(timezone.utc).date() == now_utc.astimezone(timezone.utc).date()


def snapshot(deal: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Append one observation for a rendered Deal dict; return the record.
    Skips the append (returning the existing record instead) when the latest
    observation for this ``deal_id`` already has the same ``price_eur`` and
    falls on the same UTC calendar day — a repeat display within the same day
    at an unchanged price is not a new observation."""
    now = now or datetime.now(timezone.utc)
    record = _record_from_deal(deal, now)
    prior = latest(deal["deal_id"])
    if prior is not None and prior["price_eur"] == record["price_eur"] and _same_utc_day(prior["seen_at"], now):
        return prior
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
