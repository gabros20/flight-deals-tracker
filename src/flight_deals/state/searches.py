"""Saved searches — the durable artifacts ``brief`` runs on a schedule
(SEARCH-DESIGN §4, §6; UPGRADE-PLAN §4).

A saved search is a YAML file ``data/searches/<name>.yaml``::

    schema_version: 1
    name: august-seaside
    spec: { origins: [BUD], where: "seaside & italy", depart: "2026-08", nights: "5-8" }
    schedule: "daily 08:30"        # optional -> brief picks it up
    alert: { max_price: 150, notify: telegram }   # optional -> a "watch"
    agent_prompt: |                # optional -> the agentic loop (Task 9)
      ...

A **watch** is simply a saved search carrying an ``alert`` block — there is no
separate on-disk shape, so ``searches`` and ``watch`` are two doors into the
same store. ``add`` is idempotent (same ``name`` overwrites), writes are
atomic, and the ``spec`` is validated on add so a broken search can't be saved.

``due`` compares each search's ``schedule`` against the ``last_run_at`` stamp
in ``data/searches/.runs.json`` (atomic, versioned). Schedule grammar:

* ``daily HH:MM`` — once per day at/after HH:MM (UTC);
* ``weekly DOW HH:MM`` — once per week on DOW (mon..sun) at/after HH:MM (UTC);
* ``every Nh`` — every N hours since the last run.

Clock times are interpreted in **UTC** (Global Constraint 6: state is compared
in UTC). The launchd job fires ``brief`` a few times a day; ``due`` decides
which searches actually run, so exactly-once-per-slot holds regardless of how
often ``brief`` is invoked.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flight_deals.engine.spec import SpecError, parse_spec
from flight_deals.paths import resolve_path
from flight_deals.state import store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SEARCHES_SUBDIR = "data/searches"
RUNS_FILENAME = ".runs.json"
RESULTS_SUBDIR = ".results"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class SearchError(ValueError):
    """A saved-search input error. ``hint`` is an actionable correction so the
    CLI can emit the exit-2 envelope."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# paths                                                                        #
# --------------------------------------------------------------------------- #
def searches_dir() -> Path:
    return resolve_path(SEARCHES_SUBDIR)


def _path(name: str) -> Path:
    return searches_dir() / f"{name}.yaml"


def _runs_path() -> Path:
    return searches_dir() / RUNS_FILENAME


def _result_path(name: str) -> Path:
    return searches_dir() / RESULTS_SUBDIR / f"{name}.json"


def normalize_name(name: str) -> str:
    """Slugify a proposed name and validate it (used for auto-derived names)."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower()).strip("-")
    if not slug or not _NAME_RE.match(slug):
        raise SearchError(
            f"invalid search name {name!r}",
            "use lowercase letters/digits/-/_ , e.g. --name august-seaside",
        )
    return slug


# --------------------------------------------------------------------------- #
# CRUD                                                                         #
# --------------------------------------------------------------------------- #
def add(
    *,
    name: str,
    spec: Dict[str, Any],
    schedule: Optional[str] = None,
    alert: Optional[Dict[str, Any]] = None,
    agent_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or replace (idempotent) a saved search. Validates the spec and the
    schedule before writing so a broken search never lands on disk. Returns the
    stored record."""
    name = normalize_name(name)
    try:
        parsed_spec = parse_spec(spec)  # validates; raises SpecError with a hint
    except SpecError as e:
        raise SearchError(f"saved search {name!r} has an invalid spec: {e.message}", e.hint)
    if parsed_spec.where:
        # Review item: a --where typo (e.g. "seasid & italy") that can never
        # match any destination used to save silently and only surface as a
        # wasted network call at the next scheduled run. Reject it now, the
        # same way getaway/oneway/run do before touching a provider.
        from flight_deals.engine.planner import check_where_gate
        from flight_deals.registry.destinations import DestinationRegistry
        gate = check_where_gate(parsed_spec, DestinationRegistry())
        if gate.stop and gate.exit_code == 2:
            raise SearchError(
                f"saved search {name!r} has a --where expression matching no destinations",
                (gate.env or {}).get("hint") or "",
            )
    if schedule is not None:
        parse_schedule(schedule)  # validate (raises SearchError)
    if alert is not None:
        _validate_alert(alert)

    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "spec": spec,
    }
    if schedule is not None:
        record["schedule"] = schedule
    if alert is not None:
        record["alert"] = alert
    if agent_prompt is not None:
        record["agent_prompt"] = agent_prompt

    store.atomic_write_yaml(_path(name), record)
    return record


def load(name: str) -> Optional[Dict[str, Any]]:
    """Load one saved search by name, or ``None`` if it doesn't exist."""
    return store.read_versioned(_path(name), current=SCHEMA_VERSION)


def exists(name: str) -> bool:
    return _path(name).exists()


def list_all(*, skipped: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """All saved searches, sorted by name (deterministic order for the CLI).

    SEARCH-DESIGN invites hand-editing, so a single corrupt/unparseable file must
    never abort the whole listing (and therefore never abort a brief run). A file
    that fails to load — bad YAML, a schema mismatch, or a record missing its
    ``name`` — is skipped with a logged warning; when ``skipped`` is provided each
    skip is appended as ``{"file": ..., "reason": ...}`` so the caller (brief) can
    surface it in the envelope."""
    d = searches_dir()
    if not d.exists():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(d.glob("*.yaml")):
        try:
            rec = store.read_versioned(f, current=SCHEMA_VERSION)
            if rec is not None and "name" not in rec:
                raise ValueError("saved search is missing its 'name' field")
        except Exception as e:  # noqa: BLE001 — one bad file must not sink the loop
            logger.warning("searches: skipping unreadable saved search %s: %s", f.name, e)
            if skipped is not None:
                skipped.append({"file": f.name, "reason": str(e)})
            continue
        if rec is not None:
            out.append(rec)
    return out


def remove(name: str) -> bool:
    """Delete a saved search, its run stamp, and its cached last-result."""
    name = normalize_name(name)
    p = _path(name)
    existed = p.exists()
    p.unlink(missing_ok=True)
    runs = _load_runs()
    if name in runs.get("runs", {}):
        runs["runs"].pop(name, None)
        _save_runs(runs)
    _result_path(name).unlink(missing_ok=True)
    return existed


# --------------------------------------------------------------------------- #
# last-result cache (``.results/<name>.json`` — wake's bundle, Task 9 req 4)   #
# --------------------------------------------------------------------------- #
def save_last_result(name: str, envelope: Dict[str, Any], when: datetime) -> None:
    """Persist the last envelope a saved search produced (from ``brief`` or a
    manual ``run``), so ``wake`` can bundle it without re-querying providers.
    Atomic write, versioned, one file per search — never grows unbounded."""
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    record = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "ran_at": when.astimezone(timezone.utc).isoformat(),
        "results": envelope.get("results", []),
        "summary": envelope.get("summary"),
        "sources": envelope.get("sources", {}),
    }
    if envelope.get("route_status") is not None:
        record["route_status"] = envelope["route_status"]
    store.atomic_write_json(_result_path(name), record)


def load_last_result(name: str) -> Optional[Dict[str, Any]]:
    """The last persisted result for ``name``, or ``None`` if it has never run
    (or its file is unreadable — same tolerant handling as a saved search)."""
    try:
        return store.read_versioned(_result_path(name), current=SCHEMA_VERSION)
    except store.MigrationError as e:
        logger.warning("searches: last-result cache for %r unreadable, treating as none: %s", name, e)
        return None


def is_watch(record: Dict[str, Any]) -> bool:
    """A watch is a saved search carrying an ``alert`` block."""
    return bool(record.get("alert"))


def _validate_alert(alert: Dict[str, Any]) -> None:
    if not isinstance(alert, dict) or "max_price" not in alert:
        raise SearchError(
            "alert block must set max_price",
            'alert: {max_price: 150, notify: telegram}',
        )
    try:
        mp = float(alert["max_price"])
    except (TypeError, ValueError):
        raise SearchError(
            f"alert max_price {alert['max_price']!r} is not a number",
            "alert.max_price must be a EUR amount, e.g. 150",
        )
    if mp < 0:
        raise SearchError("alert max_price cannot be negative", "alert.max_price must be >= 0")


# --------------------------------------------------------------------------- #
# run stamps (.runs.json)                                                      #
# --------------------------------------------------------------------------- #
def _load_runs() -> Dict[str, Any]:
    try:
        data = store.read_versioned(_runs_path(), current=SCHEMA_VERSION)
    except store.MigrationError as e:
        # A corrupt .runs.json is only a scheduling cache: treat it as empty
        # (every search simply looks never-run) rather than crashing the loop.
        logger.warning("searches: %s unreadable (%s) — treating as empty", RUNS_FILENAME, e)
        return {"schema_version": SCHEMA_VERSION, "runs": {}}
    if data is None:
        return {"schema_version": SCHEMA_VERSION, "runs": {}}
    data.setdefault("runs", {})
    return data


def _save_runs(data: Dict[str, Any]) -> None:
    store.atomic_write_json(_runs_path(), data)


def last_run_at(name: str) -> Optional[datetime]:
    stamp = _load_runs().get("runs", {}).get(name)
    if not stamp:
        return None
    dt = datetime.fromisoformat(stamp)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stamp_run(name: str, when: datetime) -> None:
    """Record that ``name`` ran at ``when`` (UTC). Atomic read-modify-write."""
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    runs = _load_runs()
    runs["runs"][name] = when.astimezone(timezone.utc).isoformat()
    _save_runs(runs)


def prune_stale_runs() -> List[str]:
    """Drop ``.runs`` entries whose search file no longer exists. Returns the
    removed names (brief calls this on its prune pass)."""
    runs = _load_runs()
    removed = [n for n in list(runs.get("runs", {})) if not exists(n)]
    if removed:
        for n in removed:
            runs["runs"].pop(n, None)
        _save_runs(runs)
    return removed


# --------------------------------------------------------------------------- #
# schedules                                                                    #
# --------------------------------------------------------------------------- #
class Schedule:
    """A parsed schedule. ``next_after(prev, now)`` answers "is a scheduled slot
    due?" by returning the most recent slot boundary <= now; a search is due
    when that boundary is later than its last run (or it never ran)."""

    def __init__(self, kind: str, *, at: Optional[time] = None, weekday: Optional[int] = None, hours: Optional[float] = None):
        self.kind = kind        # "daily" | "weekly" | "every"
        self.at = at
        self.weekday = weekday  # 0=mon .. 6=sun
        self.hours = hours

    def is_due(self, last_run: Optional[datetime], now: datetime) -> bool:
        now = now.astimezone(timezone.utc)
        if self.kind == "every":
            if last_run is None:
                return True
            return now - last_run.astimezone(timezone.utc) >= timedelta(hours=self.hours)

        slot = self._last_slot(now)
        if last_run is None:
            return True
        return slot > last_run.astimezone(timezone.utc)

    def _last_slot(self, now: datetime) -> datetime:
        """Most recent scheduled boundary at or before ``now`` (UTC)."""
        today_slot = datetime.combine(now.date(), self.at, tzinfo=timezone.utc)
        if self.kind == "daily":
            return today_slot if now >= today_slot else today_slot - timedelta(days=1)
        # weekly: walk back to the most recent matching weekday at/before now
        candidate = today_slot
        # how many days back to the target weekday from today
        delta_days = (now.weekday() - self.weekday) % 7
        candidate = today_slot - timedelta(days=delta_days)
        if candidate > now:
            candidate -= timedelta(days=7)
        return candidate


_SCHEDULE_HINT = (
    'schedule must be "daily HH:MM", "weekly DOW HH:MM" (DOW=mon..sun), '
    'or "every Nh", e.g. "daily 08:30"'
)


def parse_schedule(expr: str) -> Schedule:
    if not isinstance(expr, str) or not expr.strip():
        raise SearchError("schedule is empty", _SCHEDULE_HINT)
    parts = expr.strip().lower().split()
    kind = parts[0]

    if kind == "every":
        if len(parts) != 2:
            raise SearchError(f"schedule {expr!r} is malformed", _SCHEDULE_HINT)
        m = re.fullmatch(r"(\d+(?:\.\d+)?)h", parts[1])
        if not m:
            raise SearchError(f"schedule interval {parts[1]!r} must look like '6h'", _SCHEDULE_HINT)
        hours = float(m.group(1))
        if hours <= 0:
            raise SearchError("schedule interval must be > 0 hours", _SCHEDULE_HINT)
        return Schedule("every", hours=hours)

    if kind == "daily":
        if len(parts) != 2:
            raise SearchError(f"schedule {expr!r} is malformed", _SCHEDULE_HINT)
        return Schedule("daily", at=_parse_hhmm(parts[1], expr))

    if kind == "weekly":
        if len(parts) != 3:
            raise SearchError(f"schedule {expr!r} is malformed", _SCHEDULE_HINT)
        dow = parts[1][:3]
        if dow not in _WEEKDAYS:
            raise SearchError(f"schedule weekday {parts[1]!r} is not mon..sun", _SCHEDULE_HINT)
        return Schedule("weekly", at=_parse_hhmm(parts[2], expr), weekday=_WEEKDAYS.index(dow))

    raise SearchError(f"schedule {expr!r} is not recognised", _SCHEDULE_HINT)


def _parse_hhmm(raw: str, expr: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        raise SearchError(f"schedule time {raw!r} must be HH:MM", _SCHEDULE_HINT)
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise SearchError(f"schedule time {raw!r} is out of range", _SCHEDULE_HINT)
    return time(hour=h, minute=mi)


def is_due(record: Dict[str, Any], now: datetime) -> bool:
    """Is this saved search due to run at ``now``? A search with no ``schedule``
    is never automatically due (it only runs under ``brief --all``)."""
    sched = record.get("schedule")
    if not sched:
        return False
    schedule = parse_schedule(sched)
    return schedule.is_due(last_run_at(record["name"]), now)


def due(
    now: datetime,
    *,
    force_all: bool = False,
    skipped: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Saved searches due to run at ``now`` (sorted by name). ``force_all``
    returns every saved search regardless of schedule (``brief --all``).

    A search whose ``schedule`` string is malformed is skipped (logged + surfaced
    via ``skipped``) rather than aborting the whole due-check, so one hand-edited
    typo never silences every other watch."""
    records = list_all(skipped=skipped)
    if force_all:
        return records
    out: List[Dict[str, Any]] = []
    for r in records:
        try:
            if is_due(r, now):
                out.append(r)
        except SearchError as e:
            logger.warning("searches: %r has an invalid schedule, skipping: %s", r.get("name"), e.message)
            if skipped is not None:
                skipped.append({"file": f"{r.get('name')}.yaml", "reason": e.message})
    return out
