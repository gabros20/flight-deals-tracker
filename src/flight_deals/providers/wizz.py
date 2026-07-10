"""
Wizz Air provider, rebuilt on the public **timetable** endpoint with API
**version auto-discovery** (Task 4). Mirrors the Ryanair provider's contract:
typed exceptions out (no ``last_error``, no silent ``[]``), models in, parsers
raise ``SchemaError`` on shape drift.

Two facts drive the design:

* Wizz's API host is versioned — ``be.wizzair.com/{X.Y.Z}/Api/...`` — and that
  version drifts (live capture confirmed 29.5.0 -> 29.6.0). A pinned version
  eventually 404s. So the version is **auto-discovered**: resolved from a module
  cache -> ``data/wizz_version.txt`` -> a compiled-in fallback, and on a
  version-drift **404/400** the timetable page HTML is re-scraped for
  ``be.wizzair.com/{X.Y.Z}``, persisted, and the call **retried once**.
* The timetable endpoint returns **day-level minima for BOTH directions in one
  POST**, priced in the origin market currency (**HUF for BUD**). Every price is
  converted to EUR at this boundary via ``fx.to_eur`` (Global Constraint 4), and
  every fare is tagged ``price_confidence: approximate`` (±10%) — Wizz fares
  never trigger alerts directly; the estimate->confirm pipeline confirms first
  (Global Constraint 5).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flight_deals import fx, http
from flight_deals.cache import ResponseCache
from flight_deals.http import ProviderDown, SchemaError, UnexpectedStatus
from flight_deals.models import DayFare, FlightDeal
from flight_deals.paths import resolve_path

logger = logging.getLogger(__name__)

TIMETABLE_URL = "https://be.wizzair.com/{version}/Api/search/timetable"
VERSION_PAGE_URL = "https://wizzair.com/en-gb/flights/timetable"
VERSION_FILE = "data/wizz_version.txt"

CARRIER = "wizzair"
CONFIDENCE = "approximate"
SOURCE_ENDPOINT = "wizz/timetable"

# `be.wizzair.com/29.6.0` inside the page HTML; and a bare version string in the
# persisted file.
_VERSION_IN_HTML = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)")
_VERSION_BARE = re.compile(r"^\d+\.\d+\.\d+$")

# Statuses that mean "your pinned API version is wrong" — the signal to
# re-discover and retry once.
_VERSION_DRIFT_STATUSES = (404, 400)

# POST headers that worked in the live capture (see scripts/capture_fixtures.py).
# No special market cookie was required; the passenger counts in the body are
# what the endpoint validates (missing -> InvalidMinAdultCount).
_HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
}

# Module-level version cache, shared across every WizzProvider/thread and
# guarded by a lock so concurrent workers discover at most once.
_version_lock = threading.Lock()
_version_cache: Optional[str] = None


def reset_version_cache() -> None:
    """Clear the in-process version cache (tests; forces re-resolution)."""
    global _version_cache
    with _version_lock:
        _version_cache = None


@dataclass
class TimetableResult:
    """Both directions of one timetable POST, plus whether a version refresh
    happened during this call (so the orchestrator can report
    ``version_refreshed`` race-free — it's a returned value, never shared state).
    """
    outbound: List[DayFare] = field(default_factory=list)
    inbound: List[DayFare] = field(default_factory=list)
    version_refreshed: bool = False


def _hhmm(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except (ValueError, TypeError):
        return None


class WizzProvider:
    #: Compiled-in fallback if neither the module cache nor the persisted file
    #: has a version. Kept current from the last live capture; the 404-retry
    #: path recovers automatically when it drifts.
    FALLBACK_VERSION = "29.6.0"

    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.name = "wizz"
        self.use_cache = use_cache
        self._cache = ResponseCache() if use_cache else None
        # NOTE: no network in __init__ (Global Constraint / --help must be
        # offline). The version is resolved lazily on the first timetable call.

    # ------------------------------------------------------------------ #
    # Version discovery + persistence                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _version_path() -> Path:
        return resolve_path(VERSION_FILE)

    def _current_version(self) -> str:
        """Resolve the API version: module cache -> file -> fallback constant.
        Never hits the network (discovery is only triggered on a drift 404)."""
        global _version_cache
        with _version_lock:
            if _version_cache:
                return _version_cache
            path = self._version_path()
            if path.exists():
                try:
                    v = path.read_text().strip()
                    if _VERSION_BARE.match(v):
                        _version_cache = v
                        return v
                except OSError as e:
                    logger.warning("wizz: could not read %s: %s", path, e)
            _version_cache = self.FALLBACK_VERSION
            return _version_cache

    def _persist_version(self, version: str) -> None:
        global _version_cache
        with _version_lock:
            _version_cache = version
        path = self._version_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version + "\n")
        except OSError as e:
            # Non-fatal: the module cache still carries the new version for the
            # life of the process; we just couldn't persist it across runs.
            logger.warning("wizz: could not persist version to %s: %s", path, e)

    def _discover_version(self) -> str:
        """Re-scrape the timetable page HTML for the current API version.
        Raises ``ProviderDown`` if the page can't be read or no version is found."""
        html = http.get_text(VERSION_PAGE_URL, headers={"Accept": "text/html"})
        match = _VERSION_IN_HTML.search(html)
        if not match:
            raise ProviderDown(
                "wizz: version auto-discovery found no be.wizzair.com/X.Y.Z in the timetable page"
            )
        version = match.group(1)
        logger.info("wizz: discovered API version %s", version)
        self._persist_version(version)
        return version

    # ------------------------------------------------------------------ #
    # Timetable POST (both directions) with single version-refresh retry #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _payload(origin: str, dest: str, date_from: str, date_to: str) -> Dict[str, Any]:
        return {
            "flightList": [
                {"departureStation": origin, "arrivalStation": dest, "from": date_from, "to": date_to},
                {"departureStation": dest, "arrivalStation": origin, "from": date_from, "to": date_to},
            ],
            "priceType": "regular",
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
        }

    def _post_timetable(
        self, origin: str, dest: str, date_from: str, date_to: str, use_cache: bool
    ) -> tuple[Any, bool]:
        """Return ``(body, version_refreshed)``. Cache-first; on a version-drift
        404/400 re-discover the version and retry the POST exactly once."""
        key = {"origin": origin, "dest": dest, "from": date_from, "to": date_to}
        want_cache = self.use_cache and use_cache and self._cache is not None
        if want_cache:
            cached = self._cache.get(self.name, "timetable", key)
            if cached is not None:
                logger.debug("wizz: cache hit timetable %s", key)
                return cached, False

        payload = self._payload(origin, dest, date_from, date_to)
        version = self._current_version()
        refreshed = False
        try:
            body = http.post_json(TIMETABLE_URL.format(version=version), payload, headers=_HEADERS)
        except UnexpectedStatus as e:
            if e.status not in _VERSION_DRIFT_STATUSES:
                raise
            logger.warning("wizz: version %s got HTTP %s; re-discovering", version, e.status)
            new_version = self._discover_version()  # ProviderDown if it can't
            refreshed = True
            # Retry ONCE with the freshly discovered version. Any failure here
            # (incl. another drift status) propagates as a typed exception.
            body = http.post_json(TIMETABLE_URL.format(version=new_version), payload, headers=_HEADERS)

        if want_cache:
            self._cache.set(self.name, "timetable", key, body)
        return body, refreshed

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def search_timetable(
        self, origin: str, dest: str, date_from: str, date_to: str, *, use_cache: bool = True
    ) -> TimetableResult:
        """Day-level minima both directions for ``origin<->dest`` over the range,
        all prices normalized to EUR, tagged ``approximate``."""
        origin, dest = origin.upper(), dest.upper()
        body, refreshed = self._post_timetable(origin, dest, date_from, date_to, use_cache)
        if not isinstance(body, dict) or (
            "outboundFlights" not in body and "returnFlights" not in body
        ):
            raise SchemaError("wizz timetable: body missing outboundFlights/returnFlights")
        return TimetableResult(
            outbound=self._parse_flights(body.get("outboundFlights"), "outboundFlights"),
            inbound=self._parse_flights(body.get("returnFlights"), "returnFlights"),
            version_refreshed=refreshed,
        )

    def timetable(
        self, origin: str, dest: str, date_from: str, date_to: str, *, use_cache: bool = True
    ) -> tuple[List[DayFare], List[DayFare]]:
        """(outbound, inbound) day-level minima — the typed contract used by the
        planner (Task 6). See ``search_timetable`` for the version-refresh flag."""
        r = self.search_timetable(origin, dest, date_from, date_to, use_cache=use_cache)
        return r.outbound, r.inbound

    def _parse_flights(self, nodes: Any, label: str) -> List[DayFare]:
        if nodes is None:
            return []  # a one-directional request legitimately omits the other list
        if not isinstance(nodes, list):
            raise SchemaError(f"wizz timetable: '{label}' is not a list")

        out: List[DayFare] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            price = node.get("price") or node.get("originalPrice")
            if not isinstance(price, dict):
                continue  # schedule-only rows (no bookable price) — skip, not an error
            amount = price.get("amount")
            if amount is None:
                continue
            currency = price.get("currencyCode") or self.currency
            day = node.get("departureDate")
            origin = node.get("departureStation")
            dest = node.get("arrivalStation")
            if not day or not origin or not dest:
                raise SchemaError(f"wizz timetable: '{label}' row missing station/date")
            # to_eur raises UnknownCurrency (typed) on an unrecognised currency —
            # never a silent, unconverted pass-through (Global Constraint 3/4).
            price_eur = fx.to_eur(float(amount), currency)
            departure_times = node.get("departureDates") or []
            out.append(
                DayFare(
                    origin=str(origin).upper(),
                    destination=str(dest).upper(),
                    date=str(day)[:10],
                    price_eur=price_eur,
                    currency_original=str(currency).upper(),
                    price_confidence=CONFIDENCE,
                    carrier=CARRIER,
                    source_endpoint=SOURCE_ENDPOINT,
                    departure_time=_hhmm(departure_times[0]) if departure_times else None,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Compat shim for the one-way `search` / `track` CLI paths           #
    # ------------------------------------------------------------------ #
    def oneway_deals(
        self, origin: str, dest: str, date_from: str, date_to: str, use_cache: bool = True
    ) -> tuple[List[FlightDeal], bool]:
        """One-way ``origin->dest`` fares within ``[date_from, date_to]`` as
        legacy ``FlightDeal`` objects (EUR), plus the version-refresh flag so the
        orchestrator can report ``version_refreshed``. Raises typed exceptions on
        failure (no ``last_error``)."""
        if not dest:
            raise ValueError("wizz oneway_deals requires a destination")
        result = self.search_timetable(origin, dest, date_from, date_to, use_cache=use_cache)
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        deals: List[FlightDeal] = []
        for df in result.outbound:
            d = date.fromisoformat(df.date)
            if start <= d <= end:
                deals.append(
                    FlightDeal(
                        origin=df.origin,
                        destination=df.destination,
                        departure_date=df.date,
                        price=df.price_eur,  # already EUR
                        currency="EUR",
                        source="wizz",
                        notes="approximate (Wizz timetable, ±10%)",
                        source_details={
                            "price_confidence": CONFIDENCE,
                            "currency_original": df.currency_original,
                            "carrier": CARRIER,
                            "departure_time": df.departure_time,
                        },
                    )
                )
        return deals, result.version_refreshed

    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        """Legacy one-way accessor (used by the ``track`` CLI path). Delegates to
        ``oneway_deals`` and drops the refresh flag."""
        deals, _refreshed = self.oneway_deals(
            origin, destination_airport, date_from, date_to, use_cache=use_cache
        )
        return deals
