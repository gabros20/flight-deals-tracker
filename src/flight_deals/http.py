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


class UnexpectedStatus(ProviderDown):
    """
    A non-retryable, unexpected HTTP status (a 4xx that isn't 403/429 — e.g. a
    404 or 400). Subclasses ``ProviderDown`` so callers that only care that the
    call failed still treat it as a provider outage, but it carries ``.status``
    so a provider can react to a *specific* code — e.g. Wizz treats a 404/400 on
    its versioned timetable path as a signal to re-discover the API version and
    retry once (Task 4).
    """

    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(message or f"unexpected status {status}")


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

# --------------------------------------------------------------------------- #
# Session bookkeeping + a process-wide worker pool (SESSION-LIFECYCLE fix)     #
# --------------------------------------------------------------------------- #
# Task 3 review carry-over (binding): a fresh ThreadPoolExecutor per search
# spawned fresh threads, each of which lazily created a NEW per-thread
# ``requests.Session`` that was never closed — unbounded session growth and
# zero cross-search keep-alive. The fix has two parts, both here so the whole
# stack shares one policy:
#   1. Every session created by ``_session()`` is registered in
#      ``_session_registry`` so growth is observable (``session_count()``); a
#      regression test asserts it does not grow across repeated ``execute()``.
#   2. ``get_executor()`` hands out ONE process-wide pool that is reused across
#      every ``planner.execute()`` call, so its worker threads (and therefore
#      their per-thread sessions) are bounded and long-lived.
_session_lock = threading.Lock()
_session_registry: "set[requests.Session]" = set()
_executor = None  # type: ignore[var-annotated]
_executor_lock = threading.Lock()

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
        with _session_lock:
            _session_registry.add(s)
    return s


def session_count() -> int:
    """Number of live per-thread sessions created so far this process. Used by
    the session-lifecycle regression test to prove repeated ``execute()`` does
    not leak sessions (see planner)."""
    with _session_lock:
        return len(_session_registry)


def get_executor(max_workers: int = 8):
    """Return the ONE process-wide worker pool, creating it on first use.

    Reused across every ``planner.execute()`` so worker threads — and their
    per-thread ``requests.Session`` objects — are bounded and long-lived
    instead of leaking one set per search (Task 3 review carry-over).

    SIZING IS FIXED AT FIRST CREATION. ``max_workers`` on the very first call
    sizes the singleton; the pool is intentionally NOT resized on later calls
    (resizing a live ThreadPoolExecutor is not supported and would defeat the
    keep-alive point). A later call asking for a *different* size is honoured
    with the existing pool and logs a warning so the mismatch is visible rather
    than silently ignored. Call :func:`shutdown_executor` first if a genuine
    re-size is needed (e.g. in a long-lived host or a test).
    """
    global _executor
    from concurrent.futures import ThreadPoolExecutor

    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, max_workers), thread_name_prefix="fd-http"
            )
        elif max(1, max_workers) != _executor._max_workers:
            logger.warning(
                "http: worker pool already sized at %d; ignoring requested "
                "max_workers=%d (call shutdown_executor() to re-size)",
                _executor._max_workers, max_workers,
            )
        return _executor


def shutdown_executor() -> None:
    """Shut down the process-wide pool and close every per-thread session it
    created. Explicit teardown for long-lived hosts/tests; normal CLI runs let
    the pool live for the process lifetime (that's the point — keep-alive)."""
    global _executor
    with _executor_lock:
        ex = _executor
        _executor = None
    if ex is not None:
        ex.shutdown(wait=True)
    with _session_lock:
        for s in _session_registry:
            try:
                s.close()
            except Exception:  # best-effort teardown
                pass
        _session_registry.clear()
    _thread_local.session = None


def _default_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
    }


# --------------------------------------------------------------------------- #
# The one entry point                                                        #
# --------------------------------------------------------------------------- #
def _request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> requests.Response:
    """
    Shared rate-limited request core for ``get_json`` / ``post_json`` /
    ``get_text``. Retries 429/5xx up to ``max_retries`` with exponential
    backoff, then raises the appropriate typed exception. Returns the raw 200
    ``Response`` for the caller to decode (JSON vs text is the wrapper's job).

    * 403                       -> ``Blocked`` immediately (retrying won't help).
    * 429 exhausted             -> ``RateLimited``.
    * 5xx / network exhausted   -> ``ProviderDown``.
    * other 4xx (404/400/…)     -> ``UnexpectedStatus`` (carries ``.status``).
    """
    merged_headers = _default_headers()
    if headers:
        merged_headers.update(headers)

    last_status: Optional[int] = None
    for attempt in range(max_retries + 1):
        _BUCKET.acquire()
        try:
            resp = _session().request(
                method, url, params=params, json=json_body,
                headers=merged_headers, timeout=timeout,
            )
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
            return resp

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

        # Any other 4xx: not retryable, not a rate limit. Carries the status so
        # a provider (e.g. Wizz) can act on a specific code like 404.
        raise UnexpectedStatus(status, f"{url} returned unexpected status {status}")

    # Unreachable, but keeps type-checkers happy.
    raise ProviderDown(f"{url} exhausted retries (last status {last_status})")


def _decode_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError as e:
        raise SchemaError(f"200 from {resp.url} but body was not JSON: {e}") from e


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> Any:
    """Rate-limited GET returning parsed JSON (200 but not JSON -> ``SchemaError``)."""
    resp = _request(
        "GET", url, params=params, headers=headers, timeout=timeout,
        max_retries=max_retries, backoff_base=backoff_base,
    )
    return _decode_json(resp)


def post_json(
    url: str,
    json_body: Any = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> Any:
    """
    Rate-limited POST of a JSON body returning parsed JSON. Same token bucket,
    retries and typed exceptions as ``get_json`` — the Wizz timetable endpoint
    is a POST (Task 4). A non-retryable 4xx (e.g. a version-drift 404) raises
    ``UnexpectedStatus`` carrying ``.status`` so the caller can react.
    """
    resp = _request(
        "POST", url, json_body=json_body, headers=headers, timeout=timeout,
        max_retries=max_retries, backoff_base=backoff_base,
    )
    return _decode_json(resp)


def get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> str:
    """
    Rate-limited GET returning the raw response text (not JSON). Used by Wizz
    version auto-discovery, which scrapes ``be.wizzair.com/{X.Y.Z}`` out of the
    timetable page HTML.
    """
    resp = _request(
        "GET", url, params=params, headers=headers, timeout=timeout,
        max_retries=max_retries, backoff_base=backoff_base,
    )
    return resp.text
