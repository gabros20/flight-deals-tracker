import logging
from typing import Any, Dict, List

from flight_deals.providers.ryanair import RyanairProvider
from flight_deals.providers.wizz import WizzProvider
from flight_deals.config import get_config
from flight_deals import http
from flight_deals.http import Blocked, ProviderDown, RateLimited, SchemaError

logger = logging.getLogger(__name__)


def status_for_exception(exc: BaseException) -> str:
    """
    Map a typed provider/HTTP exception to a frozen ``sources`` status
    (docs/CONTRACT.md §1). Anything unexpected degrades to generic ``error`` —
    never silently to "ok"/"no results".
    """
    if isinstance(exc, (RateLimited, Blocked)):
        return "blocked"
    if isinstance(exc, SchemaError):
        return "parse_error"
    if isinstance(exc, ProviderDown):
        return "error"
    return "error"


def aggregate_status(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Merge per-call status events (each produced *locally* by the worker that
    made the call — never read from a shared, mutable provider attribute) into a
    per-provider summary. Merging happens single-threaded in the caller, so a
    failure in one of N concurrent workers can never be lost to a race
    (Task 3 req 8). "Worst status wins": any non-ok event marks the provider
    failed and records its detail.

    THE ONE implementation, imported by ``engine.planner`` (its ``execute``
    loop) and used here — there is no second copy to drift from.
    """
    ok_statuses = {"ok", "version_refreshed"}
    summary: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        name = ev["provider"]
        entry = summary.setdefault(
            name, {"ok": True, "status": "ok", "calls": 0, "errors": 0, "last_error": None}
        )
        entry["calls"] += 1
        st = ev["status"]
        if st not in ok_statuses:
            # A real failure wins over everything (incl. a version_refreshed).
            entry["ok"] = False
            entry["errors"] += 1
            entry["status"] = st
            entry["last_error"] = ev.get("detail")
        elif st == "version_refreshed" and entry["ok"] and entry["status"] == "ok":
            # Successful, but note the auto version refresh (Task 4) unless a
            # failure has already been recorded for this provider.
            entry["status"] = "version_refreshed"
    return summary


class DealOrchestrator:
    """Provider shell: holds the shared Ryanair/Wizz provider instances the
    ``track`` CLI command drives.

    The legacy concurrent category-search path (``search_by_category`` +
    cross-carrier merge + the per-provider one-way workers) was removed — the
    deterministic planner (``engine/planner.py``) is the single search path now.
    ``status_for_exception``/``aggregate_status`` above are the shared status
    primitives the planner still uses.
    """

    def __init__(self):
        self.config = get_config()
        self.ryanair = RyanairProvider()
        self.wizz = WizzProvider()
        # Align the shared rate limiter with configured policy (Constraint 9).
        http.set_rate(self.config.http_rate_per_second)
