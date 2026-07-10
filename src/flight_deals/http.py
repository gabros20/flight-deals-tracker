"""
Shared HTTP core for all providers (Global Constraint 9).

Nothing in ``providers/`` calls ``requests`` directly — everything goes through
``get_json`` here, which owns:

* a **module-level, thread-safe token-bucket rate limiter** (default ~1 req/s)
  so concurrent per-destination workers never hammer an endpoint;
* a **thread-safe session strategy** (one ``requests.Session`` per thread via
  ``threading.local`` — Sessions are not documented thread-safe, so we never
  share one across threads);
* a rotating realistic **desktop User-Agent**;
* ``<=3`` exponential-backoff **retries** on 429 / 5xx;
* **typed exceptions** so a caller (and the aggregated ``sources`` status) can
  tell *why* a call failed rather than swallowing everything into ``[]``:
  ``RateLimited`` (429 exhausted), ``Blocked`` (403 / fingerprinted),
  ``ProviderDown`` (5xx / network exhausted), ``SchemaError`` (200 whose body
  didn't match — raised by providers, defined here so the whole stack shares
  one exception hierarchy).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Typed exceptions                                                            #
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    """Base for every typed provider/HTTP failure."""


class RateLimited(ProviderError):
    """429 received and the retry budget was exhausted. Remedy: wait."""


class Blocked(ProviderError):
    """403 / fingerprint block. Distinct from a rate limit — carries the status."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(message or f"blocked (HTTP {status})")


class ProviderDown(ProviderError):
    """5xx or a network/connection error that survived all retries."""


class SchemaError(ProviderError):
    """
    A 200 response whose body didn't match the shape the provider expected.
    Raised by providers (never by ``get_json`` itself), but defined here so
    the whole stack shares one hierarchy and the orchestrator can map it to the
    ``parse_error`` status without importing provider internals.
    """


# --------------------------------------------------------------------------- #
# Rate limiter                                                                #
# --------------------------------------------------------------------------- #
class TokenBucket:
    """
    A classic token bucket. ``acquire()`` blocks (sleeps) until a token is
    available, guaranteeing an average of ``rate`` calls/second across every
    thread that shares the bucket.

    The clock and sleep functions are injectable so the limiter can be tested
    with a fake clock and **zero real sleeps** (see tests/test_http.py).
    """

    def __init__(
        self,
        rate: float = 1.0,
        capacity: float = 1.0,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._time = time_func
        self._sleep = sleep_func
        self._last = time_func()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self) -> None:
        # The lock is held across the sleep on purpose: a global limiter must
        # serialize the *spacing* decision, otherwise N threads all see "1 token
        # left" and burst together.
        with self._lock:
            self._refill(self._time())
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                self._sleep(wait)
                self._refill(self._time())
            self._tokens -= 1.0

    def set_rate(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        with self._lock:
            self._refill(self._time())
            self.rate = float(rate)


# Module-level shared limiter. ~1 req/s, small burst of 1 (Global Constraint 9).
_BUCKET = TokenBucket(rate=1.0, capacity=1.0)


def get_rate_limiter() -> TokenBucket:
    return _BUCKET


def set_rate(rate: float) -> None:
    """Reconfigure the shared limiter (used by config wiring and tests)."""
    _BUCKET.set_rate(rate)


# --------------------------------------------------------------------------- #
# Thread-safe session strategy                                               #
# --------------------------------------------------------------------------- #
_thread_local = threading.local()

# 3 rotating realistic desktop User-Agents. Ryanair/Wizz fingerprint obvious
# bot UAs; these are current-ish desktop Chrome/Safari/Firefox strings.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def _session() -> requests.Session:
    """One Session per thread (Sessions aren't documented thread-safe)."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        _thread_local.session = s
    return s


def _default_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
    }


# --------------------------------------------------------------------------- #
# The one entry point                                                        #
# --------------------------------------------------------------------------- #
def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> Any:
    """
    Rate-limited GET returning parsed JSON. Retries 429/5xx up to ``max_retries``
    with exponential backoff, then raises the appropriate typed exception.

    * 403                       -> ``Blocked`` immediately (retrying won't help).
    * 429 exhausted             -> ``RateLimited``.
    * 5xx / network exhausted   -> ``ProviderDown``.
    * 200 but not JSON          -> ``SchemaError`` (body isn't what we asked for).
    """
    merged_headers = _default_headers()
    if headers:
        merged_headers.update(headers)

    last_status: Optional[int] = None
    for attempt in range(max_retries + 1):
        _BUCKET.acquire()
        try:
            resp = _session().get(url, params=params, headers=merged_headers, timeout=timeout)
        except requests.RequestException as e:
            # Connection reset, DNS, timeout, etc. Retry, then give up.
            if attempt < max_retries:
                sleep = backoff_base * (2 ** attempt)
                logger.warning("http: %s network error (%s); retry %d in %.1fs", url, e, attempt + 1, sleep)
                time.sleep(sleep)
                continue
            raise ProviderDown(f"network error after {max_retries} retries: {e}") from e

        status = resp.status_code
        last_status = status

        if status == 200:
            try:
                return resp.json()
            except ValueError as e:
                raise SchemaError(f"200 from {url} but body was not JSON: {e}") from e

        if status == 403:
            raise Blocked(403, f"{url} returned 403 (fingerprinted/blocked)")

        if status == 429 or 500 <= status < 600:
            if attempt < max_retries:
                sleep = backoff_base * (2 ** attempt)
                logger.warning("http: %s returned %d; retry %d in %.1fs", url, status, attempt + 1, sleep)
                time.sleep(sleep)
                continue
            if status == 429:
                raise RateLimited(f"{url} returned 429 after {max_retries} retries")
            raise ProviderDown(f"{url} returned {status} after {max_retries} retries")

        # Any other 4xx: not retryable, not a rate limit.
        raise ProviderDown(f"{url} returned unexpected status {status}")

    # Unreachable, but keeps type-checkers happy.
    raise ProviderDown(f"{url} exhausted retries (last status {last_status})")
