"""
Ryanair provider, rebuilt from scratch on the stable public **farfnd v4**
endpoints (Task 3). Replaces the old ``ryanair-py``-backed client *and* folds in
``ryanair_direct.py`` — both are deleted.

Three primitives (SEARCH-DESIGN §1):

* ``roundtrip_fares`` — RT-ANYWHERE (no ``dest``) / RT-EXACT (``dest`` given):
  cheapest paired round-trips, one per destination, with flight numbers/times.
* ``cheapest_per_day`` — CAL: a month of one-way daily minima for one route.
* ``routes`` — the public route network from an airport.

Everything is EUR-requested, confidence **exact**. Failures **raise** typed
exceptions (``http.py``) — there is no ``last_error`` and no silent ``[]``. A
200 whose body doesn't match the expected schema raises ``SchemaError``.

Parsers tolerate the fixture-injected ``_truncated: true`` marker and ``size``
fields larger than the kept array (Task 2 review note).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flight_deals import http
from flight_deals.cache import ResponseCache
from flight_deals.http import SchemaError
from flight_deals.models import DayFare, FareLeg, FarePair, FlightDeal

logger = logging.getLogger(__name__)

FARFND_ROUNDTRIP = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
FARFND_ONEWAY = "https://www.ryanair.com/api/farfnd/v4/oneWayFares"
FARFND_ONEWAY_CPD = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{origin}/{dest}/cheapestPerDay"
ROUTES_URL = "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/{origin}"

MARKET = "en-gb"
CARRIER = "ryanair"
CONFIDENCE = "exact"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _hhmm(value: Optional[str]) -> Optional[str]:
    dt = _parse_dt(value)
    return dt.strftime("%H:%M") if dt else None


def _duration_minutes(dep: Optional[str], arr: Optional[str]) -> Optional[int]:
    d, a = _parse_dt(dep), _parse_dt(arr)
    if d and a:
        return int((a - d).total_seconds() // 60)
    return None


def _month_first(month: str) -> str:
    """Normalise 'YYYY-MM' or 'YYYY-MM-DD' to the first-of-month 'YYYY-MM-01'."""
    return f"{month[:7]}-01"


class RyanairProvider:
    def __init__(self, currency: str = "EUR", use_cache: bool = True):
        self.currency = currency
        self.name = CARRIER
        self.use_cache = use_cache
        self._cache = ResponseCache() if use_cache else None

    # ------------------------------------------------------------------ #
    # HTTP + cache plumbing                                              #
    # ------------------------------------------------------------------ #
    def _fetch(
        self,
        url: str,
        endpoint: str,
        query_params: Dict[str, Any],
        use_cache: bool,
        *,
        key_params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Cache-first GET of a raw response body. Typed exceptions propagate.

        ``key_params`` is what identifies the call for caching; it defaults to
        ``query_params`` but MUST be given separately when parts of the request
        live in the URL path (e.g. cheapestPerDay's origin/dest) — otherwise
        every route would collide on the same cache key.
        """
        cache_key = key_params if key_params is not None else query_params
        want_cache = self.use_cache and use_cache and self._cache is not None
        if want_cache:
            cached = self._cache.get(self.name, endpoint, cache_key)
            if cached is not None:
                logger.debug("ryanair: cache hit %s %s", endpoint, cache_key)
                return cached

        body = http.get_json(url, params=query_params)

        if want_cache:
            self._cache.set(self.name, endpoint, cache_key, body)
        return body

    # ------------------------------------------------------------------ #
    # RT-ANYWHERE / RT-EXACT                                             #
    # ------------------------------------------------------------------ #
    def roundtrip_fares(
        self,
        origin: str,
        dest: Optional[str] = None,
        *,
        out_from: str,
        out_to: str,
        duration_from: Optional[int] = None,
        duration_to: Optional[int] = None,
        ret_from: Optional[str] = None,
        ret_to: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FarePair]:
        """
        Cheapest paired round-trips. ``dest=None`` -> anywhere mode (one best
        pair per served destination). If the inbound window isn't given it is
        derived from ``out_from/out_to`` + the duration range (design decision:
        farfnd wants an inbound window; deriving it from duration keeps the
        anywhere-sweep a single argument set).
        """
        origin = origin.upper()
        if ret_from is None or ret_to is None:
            df = duration_from if duration_from is not None else 1
            dt = duration_to if duration_to is not None else df
            ret_from = ret_from or (date.fromisoformat(out_from) + timedelta(days=df)).isoformat()
            ret_to = ret_to or (date.fromisoformat(out_to) + timedelta(days=dt)).isoformat()

        params: Dict[str, Any] = {
            "departureAirportIataCode": origin,
            "outboundDepartureDateFrom": out_from,
            "outboundDepartureDateTo": out_to,
            "inboundDepartureDateFrom": ret_from,
            "inboundDepartureDateTo": ret_to,
            "currency": "EUR",
            "market": MARKET,
            "adults": 1,
        }
        if dest:
            params["arrivalAirportIataCode"] = dest.upper()
        if duration_from is not None:
            params["durationFrom"] = duration_from
        if duration_to is not None:
            params["durationTo"] = duration_to

        body = self._fetch(FARFND_ROUNDTRIP, "roundTripFares", params, use_cache)
        return self._parse_roundtrip(body, duration_from, duration_to)

    def _parse_roundtrip(
        self, body: Any, duration_from: Optional[int], duration_to: Optional[int]
    ) -> List[FarePair]:
        if not isinstance(body, dict) or "fares" not in body or not isinstance(body["fares"], list):
            raise SchemaError("roundTripFares: body missing a 'fares' list")

        pairs: List[FarePair] = []
        for fare in body["fares"]:
            try:
                out = fare["outbound"]
                inb = fare["inbound"]
                if not out or not inb:
                    continue  # anywhere mode can return one-way-only rows; skip
                outbound = self._leg(out)
                inbound = self._leg(inb)
                summary = fare.get("summary") or {}
                total = (summary.get("price") or {}).get("value")
                if total is None:
                    total = round(outbound.price_eur + inbound.price_eur, 2)
                nights = summary.get("tripDurationDays")
                if nights is None:
                    nights = (date.fromisoformat(inbound.date) - date.fromisoformat(outbound.date)).days
            except (KeyError, TypeError) as e:
                raise SchemaError(f"roundTripFares: unexpected fare shape: {e}") from e

            # Duration filtering is enforced client-side too: the anywhere sweep
            # can echo pairs at the window edges outside the requested nights.
            if duration_from is not None and nights < duration_from:
                continue
            if duration_to is not None and nights > duration_to:
                continue

            pairs.append(
                FarePair(
                    origin=outbound.origin,
                    destination=outbound.destination,
                    out_date=outbound.date,
                    return_date=inbound.date,
                    nights=int(nights),
                    total_price_eur=round(float(total), 2),
                    currency_original=(out.get("price") or {}).get("currencyCode", "EUR"),
                    price_confidence=CONFIDENCE,
                    carrier=CARRIER,
                    source_endpoint="farfnd/roundTripFares",
                    outbound=outbound,
                    inbound=inbound,
                )
            )
        return pairs

    @staticmethod
    def _leg(node: Dict[str, Any]) -> FareLeg:
        price = node.get("price") or {}
        dep = node.get("departureDate")
        return FareLeg(
            origin=node["departureAirport"]["iataCode"],
            destination=node["arrivalAirport"]["iataCode"],
            date=str(dep)[:10],
            price_eur=round(float(price["value"]), 2),
            carrier=CARRIER,
            departure_time=_hhmm(dep),
            arrival_time=_hhmm(node.get("arrivalDate")),
            flight_number=node.get("flightNumber"),
            duration_minutes=_duration_minutes(dep, node.get("arrivalDate")),
        )

    # ------------------------------------------------------------------ #
    # OW-ANYWHERE / OW-EXACT — one-way fares (Task 7, S1)                #
    # ------------------------------------------------------------------ #
    def oneway_fares(
        self,
        origin: str,
        dest: Optional[str] = None,
        *,
        out_from: str,
        out_to: str,
        use_cache: bool = True,
    ) -> List[DayFare]:
        """Cheapest one-way fare per destination (``dest=None`` -> anywhere) in
        the outbound window, from farfnd ``oneWayFares``. Mirrors
        :meth:`roundtrip_fares` but returns per-destination outbound
        :class:`DayFare` legs (exact confidence). Shape matches roundTripFares:
        a ``fares`` list whose entries carry an ``outbound`` leg node."""
        origin = origin.upper()
        params: Dict[str, Any] = {
            "departureAirportIataCode": origin,
            "outboundDepartureDateFrom": out_from,
            "outboundDepartureDateTo": out_to,
            "currency": "EUR",
            "market": MARKET,
            "adults": 1,
        }
        key = dict(params)
        if dest:
            params["arrivalAirportIataCode"] = dest.upper()
            key["arrivalAirportIataCode"] = dest.upper()
        body = self._fetch(FARFND_ONEWAY, "oneWayFares", params, use_cache, key_params=key)
        return self._parse_oneway(body)

    def _parse_oneway(self, body: Any) -> List[DayFare]:
        if not isinstance(body, dict) or not isinstance(body.get("fares"), list):
            raise SchemaError("oneWayFares: body missing a 'fares' list")
        out: List[DayFare] = []
        for fare in body["fares"]:
            try:
                node = fare.get("outbound") if isinstance(fare, dict) else None
                if not node:
                    continue
                leg = self._leg(node)
            except (KeyError, TypeError) as e:
                raise SchemaError(f"oneWayFares: unexpected fare shape: {e}") from e
            out.append(
                DayFare(
                    origin=leg.origin,
                    destination=leg.destination,
                    date=leg.date,
                    price_eur=leg.price_eur,
                    currency_original=(node.get("price") or {}).get("currencyCode", "EUR"),
                    price_confidence=CONFIDENCE,
                    carrier=CARRIER,
                    source_endpoint="farfnd/oneWayFares",
                    departure_time=leg.departure_time,
                    flight_number=leg.flight_number,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # CAL — cheapest per day                                             #
    # ------------------------------------------------------------------ #
    def cheapest_per_day(
        self, origin: str, dest: str, month: str, *, use_cache: bool = True
    ) -> List[DayFare]:
        """A month of one-way daily minima for one route+direction."""
        origin, dest = origin.upper(), dest.upper()
        url = FARFND_ONEWAY_CPD.format(origin=origin, dest=dest)
        params = {"outboundMonthOfDate": _month_first(month), "currency": "EUR"}
        # origin/dest are URL path params — include them in the cache key so
        # different routes don't collide on the same (month, currency) key.
        key = {"origin": origin, "dest": dest, **params}
        body = self._fetch(url, "cheapestPerDay", params, use_cache, key_params=key)
        return self._parse_cheapest_per_day(body, origin, dest)

    def _parse_cheapest_per_day(self, body: Any, origin: str, dest: str) -> List[DayFare]:
        if not isinstance(body, dict) or not isinstance(body.get("outbound"), dict):
            raise SchemaError("cheapestPerDay: body missing 'outbound' object")
        fares = body["outbound"].get("fares")
        if not isinstance(fares, list):
            raise SchemaError("cheapestPerDay: 'outbound.fares' is not a list")

        out: List[DayFare] = []
        for f in fares:
            if not isinstance(f, dict):
                continue
            if f.get("unavailable") or f.get("soldOut"):
                continue
            price = f.get("price")
            day = f.get("day")
            if not price or price.get("value") is None or not day:
                continue
            out.append(
                DayFare(
                    origin=origin,
                    destination=dest,
                    date=str(day)[:10],
                    price_eur=round(float(price["value"]), 2),
                    currency_original=price.get("currencyCode", "EUR"),
                    price_confidence=CONFIDENCE,
                    carrier=CARRIER,
                    source_endpoint="farfnd/oneWayFares/cheapestPerDay",
                    departure_time=_hhmm(f.get("departureDate")),
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Route network                                                     #
    # ------------------------------------------------------------------ #
    def routes(self, origin: str, *, use_cache: bool = True) -> List[str]:
        """
        Destinations flyable from ``origin`` on Ryanair. Public endpoint shape
        (documented; no live fixture was captured at freeze time): a JSON list
        of route objects, each with ``arrivalAirport.code`` (IATA). We tolerate
        both ``code`` and ``iataCode``.
        """
        origin = origin.upper()
        url = ROUTES_URL.format(origin=origin)
        body = self._fetch(url, "routes", {"origin": origin}, use_cache)
        if not isinstance(body, list):
            raise SchemaError("routes: expected a JSON list of route objects")

        codes = set()
        for entry in body:
            if not isinstance(entry, dict):
                continue
            arr = entry.get("arrivalAirport") or {}
            code = arr.get("code") or arr.get("iataCode")
            if code:
                codes.add(str(code).upper())
        return sorted(codes)

    # ------------------------------------------------------------------ #
    # Compatibility shim for the one-way `search` / `track` CLI paths    #
    # ------------------------------------------------------------------ #
    def get_cheapest_flights(
        self,
        origin: str,
        date_from: str,
        date_to: str,
        destination_airport: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[FlightDeal]:
        """
        One-way daily minima for ``origin -> destination`` within
        ``[date_from, date_to]``, as legacy ``FlightDeal`` objects so the
        existing CLI renderer keeps working. Built on ``cheapest_per_day``.
        Raises typed exceptions on failure (no ``last_error``).
        """
        if not destination_airport:
            raise ValueError("get_cheapest_flights requires a destination (one-way, per-route)")
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)

        deals: List[FlightDeal] = []
        for month in _months_spanning(start, end):
            day_fares = self.cheapest_per_day(origin, destination_airport, month, use_cache=use_cache)
            for df in day_fares:
                d = date.fromisoformat(df.date)
                if start <= d <= end:
                    deals.append(
                        FlightDeal(
                            origin=df.origin,
                            destination=df.destination,
                            departure_date=df.date,
                            price=df.price_eur,
                            currency=df.currency_original,
                            source=CARRIER,
                        )
                    )
        return deals


def _months_spanning(start: date, end: date) -> List[str]:
    months, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return months
