#!/usr/bin/env python3
"""
Manual, one-shot capture of REAL provider responses into tests/fixtures/.

Run by hand (never in CI, never by the test suite — Global Constraint 10):

    .venv/bin/python scripts/capture_fixtures.py
    .venv/bin/python scripts/capture_fixtures.py --out-dir tests/fixtures --sleep 2.0

This is Phase 0.5 (docs/UPGRADE-PLAN.md §7, docs/CONTRACT.md): the schema and
these fixtures must exist BEFORE Tasks 3-8 rebuild the providers, so later
tests validate against real recorded shapes instead of invented ones.

Endpoints hit (known-good per docs/RESEARCH.md §15 and
src/flight_deals/providers/ryanair_direct.py):
  - farfnd v4 roundTripFares (exact, anywhere-mode, 200-but-empty)
  - farfnd v4 oneWayFares/{O}/{D}/cheapestPerDay (both directions)
  - wizzair.com timetable HTML (version discovery snippet)
  - be.wizzair.com/{version}/Api/search/timetable (both directions in one
    POST) + a deliberately wrong version to capture the 404 body

Politeness: >=2s between requests (--sleep), realistic rotating desktop
User-Agent strings, a single retry-free attempt per fixture (this is a
recorder, not a resilient client — Task 3/4 build the real retry logic). On
403/429/connection failure we give up on that ONE fixture, log why, and
write a clearly-marked synthetic placeholder instead of failing the whole
run. Nothing here is sanitized (all of this is public, unauthenticated
data) except huge arrays, which are truncated to <=20 entries with a
"_truncated": true marker alongside the truncated list.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("capture_fixtures")

# A couple of realistic desktop UA strings to rotate through, per
# docs/UPGRADE-PLAN.md §3 hardening note (adambenhassen/ryanair-mcp pattern).
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

FARFND_ROUNDTRIP_URL = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
FARFND_CHEAPEST_PER_DAY_URL = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{origin}/{dest}/cheapestPerDay"
WIZZ_VERSION_PAGE_URL = "https://wizzair.com/en-gb/flights/timetable"
WIZZ_TIMETABLE_URL = "https://be.wizzair.com/{version}/Api/search/timetable"
WIZZ_FALLBACK_VERSION_KNOWN_WRONG = "0.0.0"

MAX_ARRAY_ENTRIES = 20


class Capture:
    """One HTTP session + a shared rate-limited `get`/`post`, used only by
    this script. Not the shared http.py (that's Task 3) — this is a
    deliberately standalone, throwaway recorder."""

    def __init__(self, sleep_seconds: float):
        self.session = requests.Session()
        self.sleep_seconds = sleep_seconds
        self._ua_index = 0
        self._last_request_at: float | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        headers = {
            "User-Agent": ua,
            "Accept-Language": "en-GB,en;q=0.9",
        }
        if extra:
            headers.update(extra)
        return headers

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.sleep_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 15):
        self._throttle()
        return self.session.get(url, params=params, headers=self._headers(headers), timeout=timeout)

    def post(self, url: str, json_body: dict, headers: dict | None = None, timeout: int = 20):
        self._throttle()
        return self.session.post(url, json=json_body, headers=self._headers(headers), timeout=timeout)


def truncate(obj: Any, limit: int = MAX_ARRAY_ENTRIES) -> Any:
    """Recursively cap any list to `limit` entries, tagging the container
    dict it lives in with "_truncated": true. Only meaningful on dicts
    containing lists; a bare top-level list gets wrapped."""
    if isinstance(obj, dict):
        out = {}
        truncated_any = False
        for k, v in obj.items():
            if isinstance(v, list) and len(v) > limit:
                out[k] = [truncate(x, limit) for x in v[:limit]]
                truncated_any = True
            else:
                out[k] = truncate(v, limit)
        if truncated_any:
            out["_truncated"] = True
        return out
    if isinstance(obj, list):
        return [truncate(x, limit) for x in obj]
    return obj


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("wrote %s", path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info("wrote %s", path)


def next_saturday(offset_weeks: int = 6) -> date:
    """A date comfortably in the future (season + demand alive), landing on
    a weekend so a scheduled seasonal route is more likely to be operating."""
    d = date.today() + timedelta(weeks=offset_weeks)
    return d + timedelta(days=(5 - d.weekday()) % 7)  # 5 = Saturday


# ---------------------------------------------------------------------------
# Individual fixture captures. Each returns a short status dict for the
# end-of-run summary; each is independently try/except'd so one failure
# (e.g. Wizz blocking) never kills the rest of the run.
# ---------------------------------------------------------------------------

def capture_farfnd_roundtrip_exact(cap: Capture, out_dir: Path) -> dict:
    name = "farfnd_roundtrip_exact_bud_cfu"
    out_date = next_saturday()
    ret_date = out_date + timedelta(days=5)
    params = {
        "departureAirportIataCode": "BUD",
        "arrivalAirportIataCode": "CFU",
        "outboundDepartureDateFrom": out_date.isoformat(),
        "outboundDepartureDateTo": out_date.isoformat(),
        "inboundDepartureDateFrom": ret_date.isoformat(),
        "inboundDepartureDateTo": ret_date.isoformat(),
        "currency": "EUR",
        "market": "en-gb",
        "adults": 1,
    }
    try:
        resp = cap.get(FARFND_ROUNDTRIP_URL, params=params, headers={"Accept": "application/json", "Referer": "https://www.ryanair.com/"})
        body = safe_json(resp)
        write_json(out_dir / f"{name}.json", {
            "_captured_live": True,
            "_url": FARFND_ROUNDTRIP_URL,
            "_params": params,
            "_status_code": resp.status_code,
            "body": truncate(body) if body is not None else None,
            "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
        })
        return {"name": name, "status": "captured", "http_status": resp.status_code}
    except requests.RequestException as e:
        return synthetic_fallback(out_dir, name, params, FARFND_ROUNDTRIP_URL, reason=str(e), body=SYNTHETIC_FARFND_ROUNDTRIP)


def capture_farfnd_roundtrip_anywhere(cap: Capture, out_dir: Path) -> dict:
    name = "farfnd_roundtrip_anywhere_bud"
    out_from = next_saturday()
    out_to = out_from + timedelta(days=2)
    ret_from = out_from + timedelta(days=5)
    ret_to = out_to + timedelta(days=9)
    params = {
        "departureAirportIataCode": "BUD",
        "outboundDepartureDateFrom": out_from.isoformat(),
        "outboundDepartureDateTo": out_to.isoformat(),
        # farfnd rejects anywhere-mode without an inbound window too (400
        # NotNull on inboundDepartureDateFrom/To when only durationFrom/To
        # are given) — confirmed live during this capture run.
        "inboundDepartureDateFrom": ret_from.isoformat(),
        "inboundDepartureDateTo": ret_to.isoformat(),
        "durationFrom": 5,
        "durationTo": 8,
        "currency": "EUR",
        "market": "en-gb",
        "adults": 1,
    }
    try:
        resp = cap.get(FARFND_ROUNDTRIP_URL, params=params, headers={"Accept": "application/json", "Referer": "https://www.ryanair.com/"})
        body = safe_json(resp)
        write_json(out_dir / f"{name}.json", {
            "_captured_live": True,
            "_url": FARFND_ROUNDTRIP_URL,
            "_params": params,
            "_status_code": resp.status_code,
            "body": truncate(body) if body is not None else None,
            "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
        })
        return {"name": name, "status": "captured", "http_status": resp.status_code}
    except requests.RequestException as e:
        return synthetic_fallback(out_dir, name, params, FARFND_ROUNDTRIP_URL, reason=str(e), body=SYNTHETIC_FARFND_ANYWHERE)


def capture_farfnd_cheapest_per_day(cap: Capture, out_dir: Path) -> dict:
    results = []
    month = next_saturday().replace(day=1)
    for origin, dest in (("BUD", "CFU"), ("CFU", "BUD")):
        name = f"farfnd_cheapest_per_day_{origin.lower()}_{dest.lower()}"
        url = FARFND_CHEAPEST_PER_DAY_URL.format(origin=origin, dest=dest)
        params = {"outboundMonthOfDate": month.isoformat(), "currency": "EUR"}
        try:
            resp = cap.get(url, params=params, headers={"Accept": "application/json", "Referer": "https://www.ryanair.com/"})
            body = safe_json(resp)
            write_json(out_dir / f"{name}.json", {
                "_captured_live": True,
                "_url": url,
                "_params": params,
                "_status_code": resp.status_code,
                "body": truncate(body) if body is not None else None,
                "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
            })
            results.append({"name": name, "status": "captured", "http_status": resp.status_code})
        except requests.RequestException as e:
            results.append(synthetic_fallback(out_dir, name, params, url, reason=str(e), body=SYNTHETIC_FARFND_CHEAPEST_PER_DAY))
    return {"name": "farfnd_cheapest_per_day", "status": "captured", "sub": results}


def capture_farfnd_empty_nonexistent(cap: Capture, out_dir: Path) -> dict:
    name = "farfnd_roundtrip_empty_nonexistent"
    out_date = next_saturday()
    ret_date = out_date + timedelta(days=5)
    # BUD->JFK: not a Ryanair-served route; expected to 200 with an empty
    # fares array rather than 404, per farfnd's usual "no results" shape.
    params = {
        "departureAirportIataCode": "BUD",
        "arrivalAirportIataCode": "JFK",
        "outboundDepartureDateFrom": out_date.isoformat(),
        "outboundDepartureDateTo": out_date.isoformat(),
        "inboundDepartureDateFrom": ret_date.isoformat(),
        "inboundDepartureDateTo": ret_date.isoformat(),
        "currency": "EUR",
        "market": "en-gb",
        "adults": 1,
    }
    try:
        resp = cap.get(FARFND_ROUNDTRIP_URL, params=params, headers={"Accept": "application/json", "Referer": "https://www.ryanair.com/"})
        body = safe_json(resp)
        write_json(out_dir / f"{name}.json", {
            "_captured_live": True,
            "_url": FARFND_ROUNDTRIP_URL,
            "_params": params,
            "_status_code": resp.status_code,
            "body": truncate(body) if body is not None else None,
            "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
        })
        return {"name": name, "status": "captured", "http_status": resp.status_code}
    except requests.RequestException as e:
        return synthetic_fallback(out_dir, name, params, FARFND_ROUNDTRIP_URL, reason=str(e), body=SYNTHETIC_FARFND_EMPTY)


def capture_wizz_version_snippet(cap: Capture, out_dir: Path) -> tuple[dict, str | None]:
    """Capture just the HTML region around `be.wizzair.com/X.Y.Z` (the
    version auto-discovery source), and return the discovered version so
    later Wizz fixture captures use a real, current version string."""
    name = "wizz_version_discovery_snippet"
    try:
        resp = cap.get(WIZZ_VERSION_PAGE_URL, headers={"Accept": "text/html"})
        text = resp.text
        match = re.search(r"be\.wizzair\.com/(\d+\.\d+\.\d+)", text)
        if match:
            start = max(match.start() - 200, 0)
            end = min(match.end() + 200, len(text))
            snippet = text[start:end]
            write_text(out_dir / f"{name}.html", (
                f"<!-- _captured_live: true; _url: {WIZZ_VERSION_PAGE_URL}; "
                f"_status_code: {resp.status_code}; discovered_version: {match.group(1)} -->\n"
                f"{snippet}\n"
            ))
            return {"name": name, "status": "captured", "http_status": resp.status_code, "version": match.group(1)}, match.group(1)
        # 200 but the pattern wasn't found — capture what we got so a human
        # can see why, and fall back to the known-good pinned version.
        write_text(out_dir / f"{name}.html", (
            f"<!-- _captured_live: true; _url: {WIZZ_VERSION_PAGE_URL}; "
            f"_status_code: {resp.status_code}; PATTERN_NOT_FOUND: true -->\n"
            f"{text[:2000]}\n"
        ))
        return {"name": name, "status": "captured_no_match", "http_status": resp.status_code}, None
    except requests.RequestException as e:
        logger.warning("wizz version discovery failed: %s", e)
        write_text(out_dir / f"{name}.html", (
            f"<!-- _synthetic: true; _synthetic_reason: {e!s}; "
            f"documented per docs/RESEARCH.md section 15 / providers/wizz.py FALLBACK_VERSION -->\n"
            f"<script>var apiHost = \"be.wizzair.com/29.5.0\";</script>\n"
        ))
        return {"name": name, "status": "synthetic", "reason": str(e)}, None


def capture_wizz_timetable(cap: Capture, out_dir: Path, version: str) -> dict:
    name = "wizz_timetable_bud_cta"
    date_from = next_saturday()
    date_to = date_from + timedelta(days=14)
    url = WIZZ_TIMETABLE_URL.format(version=version)
    payload = {
        "flightList": [
            {"departureStation": "BUD", "arrivalStation": "CTA", "from": date_from.isoformat(), "to": date_to.isoformat()},
            {"departureStation": "CTA", "arrivalStation": "BUD", "from": date_from.isoformat(), "to": date_to.isoformat()},
        ],
        "priceType": "regular",
        # Wizz rejects the request with validationCodes:["InvalidMinAdultCount"]
        # without passenger counts — confirmed live during this capture run.
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
    }
    headers = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json, text/plain, */*"}
    try:
        resp = cap.post(url, json_body=payload, headers=headers)
        body = safe_json(resp)
        write_json(out_dir / f"{name}.json", {
            "_captured_live": True,
            "_url": url,
            "_payload": payload,
            "_status_code": resp.status_code,
            "body": truncate(body) if body is not None else None,
            "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
        })
        return {"name": name, "status": "captured", "http_status": resp.status_code}
    except requests.RequestException as e:
        return synthetic_fallback(out_dir, name, payload, url, reason=str(e), body=SYNTHETIC_WIZZ_TIMETABLE, is_post=True)


def capture_wizz_wrong_version_404(cap: Capture, out_dir: Path) -> dict:
    name = "wizz_wrong_version_404"
    date_from = next_saturday()
    date_to = date_from + timedelta(days=14)
    url = WIZZ_TIMETABLE_URL.format(version=WIZZ_FALLBACK_VERSION_KNOWN_WRONG)
    payload = {
        "flightList": [
            {"departureStation": "BUD", "arrivalStation": "CTA", "from": date_from.isoformat(), "to": date_to.isoformat()},
        ],
        "priceType": "regular",
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
    }
    headers = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json, text/plain, */*"}
    try:
        resp = cap.post(url, json_body=payload, headers=headers)
        # We WANT a non-200 here (404, by design, since 0.0.0 is never a
        # real version) — that's the fixture. A 200 would mean Wizz changed
        # behavior; still capture it, verbatim, and note the surprise.
        body = safe_json(resp)
        write_json(out_dir / f"{name}.json", {
            "_captured_live": True,
            "_url": url,
            "_payload": payload,
            "_status_code": resp.status_code,
            "_note": "expected non-200 (this uses a deliberately invalid version string)" if resp.status_code == 200 else None,
            "body": truncate(body) if body is not None else None,
            "_raw_text_on_parse_failure": None if body is not None else resp.text[:2000],
        })
        return {"name": name, "status": "captured", "http_status": resp.status_code}
    except requests.RequestException as e:
        return synthetic_fallback(out_dir, name, payload, url, reason=str(e), body=SYNTHETIC_WIZZ_404, is_post=True)


def safe_json(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return None


def synthetic_fallback(out_dir: Path, name: str, params_or_payload: dict, url: str, reason: str, body: dict, is_post: bool = False) -> dict:
    logger.warning("%s: live capture failed (%s); writing synthetic placeholder", name, reason)
    payload = {
        "_synthetic": True,
        "_synthetic_reason": reason,
        "_url": url,
        ("_payload" if is_post else "_params"): params_or_payload,
        "body": body,
    }
    write_json(out_dir / f"{name}.json", payload)
    return {"name": name, "status": "synthetic", "reason": reason}


# ---------------------------------------------------------------------------
# Synthetic placeholder bodies, hand-derived from docs/RESEARCH.md §15 and
# the existing (working, live-tested per that section) parse logic in
# providers/ryanair_direct.py and providers/wizz.py — used ONLY if the live
# capture above fails for that fixture.
# ---------------------------------------------------------------------------

SYNTHETIC_FARFND_ROUNDTRIP = {
    "fares": [
        {
            "outbound": {
                "departureAirport": {"iataCode": "BUD", "name": "Budapest", "countryName": "Hungary"},
                "arrivalAirport": {"iataCode": "CFU", "name": "Corfu", "countryName": "Greece"},
                "departureDate": "2026-08-22T10:35:00",
                "flightNumber": "FR1234",
                "price": {"value": 44.99, "currencyCode": "EUR"},
            },
            "inbound": {
                "departureAirport": {"iataCode": "CFU", "name": "Corfu", "countryName": "Greece"},
                "arrivalAirport": {"iataCode": "BUD", "name": "Budapest", "countryName": "Hungary"},
                "departureDate": "2026-08-27T22:10:00",
                "flightNumber": "FR1235",
                "price": {"value": 44.99, "currencyCode": "EUR"},
            },
        }
    ]
}

SYNTHETIC_FARFND_ANYWHERE = {
    "fares": [
        {
            "outbound": {
                "departureAirport": {"iataCode": "BUD", "name": "Budapest", "countryName": "Hungary"},
                "arrivalAirport": {"iataCode": "CTA", "name": "Catania", "countryName": "Italy"},
                "departureDate": "2026-08-22T06:15:00",
                "flightNumber": "FR2201",
                "price": {"value": 39.99, "currencyCode": "EUR"},
            },
            "inbound": {
                "departureAirport": {"iataCode": "CTA", "name": "Catania", "countryName": "Italy"},
                "arrivalAirport": {"iataCode": "BUD", "name": "Budapest", "countryName": "Hungary"},
                "departureDate": "2026-08-27T21:05:00",
                "flightNumber": "FR2202",
                "price": {"value": 42.49, "currencyCode": "EUR"},
            },
        }
    ]
}

SYNTHETIC_FARFND_CHEAPEST_PER_DAY = {
    "outbound": {
        "fares": [
            {"day": "2026-08-01", "arrivalDate": "2026-08-01T18:50:00", "departureDate": "2026-08-01T16:10:00",
             "price": {"value": 44.99, "currencyCode": "EUR"}, "unavailable": False, "soldOut": False},
            {"day": "2026-08-02", "arrivalDate": None, "departureDate": None,
             "price": None, "unavailable": True, "soldOut": False},
        ]
    }
}

SYNTHETIC_FARFND_EMPTY = {"fares": []}

SYNTHETIC_WIZZ_TIMETABLE = {
    "outboundFlights": [
        {
            "departureStation": "BUD",
            "arrivalStation": "CTA",
            "departureDate": "2026-08-22T00:00:00",
            "price": {"amount": 25990, "currencyCode": "HUF", "exchangedAmount": None, "exchangedCurrencyCode": None},
            "priceType": "price",
        }
    ],
    "returnFlights": [
        {
            "departureStation": "CTA",
            "arrivalStation": "BUD",
            "departureDate": "2026-08-29T00:00:00",
            "price": {"amount": 27990, "currencyCode": "HUF", "exchangedAmount": None, "exchangedCurrencyCode": None},
            "priceType": "price",
        }
    ],
}

SYNTHETIC_WIZZ_404 = {"message": "Not Found", "statusCode": 404}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--sleep", type=float, default=2.0, help="minimum seconds between requests")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    out_dir = args.out_dir
    cap = Capture(sleep_seconds=args.sleep)

    summary: list[dict] = []

    version_result, version = capture_wizz_version_snippet(cap, out_dir)
    summary.append(version_result)
    if version is None:
        version = "29.5.0"  # documented current-ish pin, per docs/RESEARCH.md §15 discussion
        logger.warning("using fallback version %s for the timetable fixture capture", version)

    summary.append(capture_farfnd_roundtrip_exact(cap, out_dir))
    summary.append(capture_farfnd_roundtrip_anywhere(cap, out_dir))
    summary.append(capture_farfnd_cheapest_per_day(cap, out_dir))
    summary.append(capture_farfnd_empty_nonexistent(cap, out_dir))
    summary.append(capture_wizz_timetable(cap, out_dir, version))
    summary.append(capture_wizz_wrong_version_404(cap, out_dir))

    # Flatten the cheapest-per-day sub-results for the summary table.
    flat: list[dict] = []
    for item in summary:
        if item.get("name") == "farfnd_cheapest_per_day" and "sub" in item:
            flat.extend(item["sub"])
        else:
            flat.append(item)

    live = [x for x in flat if x["status"] not in ("synthetic",)]
    synthetic = [x for x in flat if x["status"] == "synthetic"]

    print("\n--- capture_fixtures.py summary ---", file=sys.stderr)
    for item in flat:
        print(f"  {item['name']}: {item['status']}" + (f" (http {item.get('http_status')})" if item.get("http_status") else "") + (f" [{item.get('reason')}]" if item.get("reason") else ""), file=sys.stderr)
    print(f"captured live: {len(live)}, synthetic placeholders: {len(synthetic)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
