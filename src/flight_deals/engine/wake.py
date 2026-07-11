"""``wake <name>`` — bundle a saved search for an agentic review session
(SEARCH-DESIGN §6 "Scheduled search: deterministic core + agentic periphery",
Task 9 req 4).

The deterministic loop (``brief``) never needs a model in it — alerts flow
regardless. The agentic loop is a SEPARATE, cheap-to-skip periphery: a saved
search that carries an ``agent_prompt`` also appears in
``flight-deals searches due --agentic``; a scheduled Hermes/Claude session
runs ``flight-deals wake <name>`` and gets ONE self-contained bundle so it
never has to re-derive context by re-running the search itself:

* the saved search's ``spec`` and ``agent_prompt``;
* the last persisted run (``state.searches.load_last_result`` — written by
  ``brief`` after every execution, see ``state/searches.py``);
* history context for the routes that last run actually returned
  (``history.compare``, the same comparison ``combine.enrich`` uses);
* the FIXED list of sandboxed spec-mutation moves the agent is invited to try
  (``ALLOWED_MOVES`` below) — its creativity is bounded to these plus a
  messaging decision, so a weak week just means "no news", never a broken
  pipeline (SEARCH-DESIGN §6).

``wake`` never touches the network itself — it only reads saved state. If the
agent wants fresh numbers it re-runs the spec via ``flight-deals run --spec``
(one of the allowed moves) and can persist a variation with
``flight-deals searches add --name <name> --spec <file|->``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from flight_deals import output
from flight_deals.history import PriceHistoryStore
from flight_deals.state import searches

# The fixed vocabulary of sandboxed spec-mutation moves (SEARCH-DESIGN §6).
# These are the ONLY shapes of change the agentic loop is invited to make —
# never hand-edit a saved search's YAML, never invent a new kind of mutation.
ALLOWED_MOVES: List[Dict[str, str]] = [
    {
        "move": "shift_window",
        "description": "Shift the depart window a few days earlier or later (e.g. +/-3 days).",
    },
    {
        "move": "widen_nights",
        "description": "Broaden the nights range by 1-2 nights on either side.",
    },
    {
        "move": "swap_where_tag",
        "description": 'Substitute one tag in the where expression for a comparable sibling '
                        '(e.g. "greece" -> "croatia"); run `flight-deals where list` first if unsure.',
    },
    {
        "move": "adjust_budget",
        "description": "Raise or lower budget by a modest amount (10-20%) to see how many more/fewer deals qualify.",
    },
    {
        "move": "message_decision",
        "description": "Decide whether anything found is worth a message. A quiet week means no message — never fabricate news.",
    },
    {
        "move": "persist_variation",
        "description": "If a variation is worth keeping, save it with "
                        "`flight-deals searches add --name <name> --spec <file|-> ` (idempotent overwrite). "
                        "Sanity-check cost with `flight-deals plan --spec ...` first — never hand-edit the YAML.",
    },
]

MAX_HISTORY_ROUTES = 5


def _history_context(last_result: Optional[Dict[str, Any]], history_store: PriceHistoryStore) -> List[Dict[str, Any]]:
    """Per-route history comparison (``history.compare``) for the routes the
    last persisted run actually returned — grounded in real observations
    rather than speculative coverage. Empty when there is no last run."""
    if not last_result:
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for deal in last_result.get("results", [])[:MAX_HISTORY_ROUTES]:
        origin, dest = deal.get("origin"), deal.get("destination")
        if not origin or not dest or (origin, dest) in seen:
            continue
        seen.add((origin, dest))
        try:
            compare = history_store.compare(origin, dest, float(deal["price_eur"]))
        except Exception:  # noqa: BLE001 — history is a convenience, never fatal to wake
            continue
        out.append({"origin": origin, "destination": dest, "compare": compare})
    return out


def build_wake(name: str, *, history_store: Optional[PriceHistoryStore] = None) -> Tuple[Dict[str, Any], int]:
    """Build the ``wake`` envelope for a saved search. Returns
    ``(envelope, exit_code)`` — exit 2 with ``error``/``hint`` for an unknown
    name (CONTRACT §3), exit 0 otherwise (a search that has simply never run
    is not a failure)."""
    record = searches.load(name)
    if record is None:
        env = output.error_envelope(
            "unknown_search",
            f"no saved search named {name!r} — run 'flight-deals searches list'",
        )
        return env, 2

    history_store = history_store or PriceHistoryStore()
    last_result = searches.load_last_result(record["name"])
    history = _history_context(last_result, history_store)

    if last_result is None:
        summary = (
            f"{record['name']}: never run yet — run 'flight-deals run --spec "
            f"{record['name']}.yaml' or wait for the next scheduled brief."
        )
        results: List[Dict[str, Any]] = []
        sources: Dict[str, str] = {}
        route_status = None
    else:
        n = len(last_result.get("results", []))
        summary = (
            f"{record['name']}: last run at {last_result.get('ran_at')} found "
            f"{n} deal{'s' if n != 1 else ''}."
        )
        results = last_result.get("results", [])
        sources = last_result.get("sources", {})
        route_status = last_result.get("route_status")

    env = output.envelope(
        results=results,
        summary=summary,
        sources=sources,
        next=[f"flight-deals searches show {record['name']}"],
        route_status=route_status,
    )
    env["name"] = record["name"]
    env["spec"] = record["spec"]
    env["schedule"] = record.get("schedule")
    env["alert"] = record.get("alert")
    env["agent_prompt"] = record.get("agent_prompt")
    env["last_result"] = last_result
    env["history"] = history
    env["allowed_moves"] = ALLOWED_MOVES
    return env, 0
