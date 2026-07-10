"""Tests for the shared HTTP core: rate limiter, retries, typed exceptions."""

import threading

import pytest
import responses

from flight_deals import http
from flight_deals.http import (
    Blocked,
    ProviderDown,
    RateLimited,
    TokenBucket,
    get_json,
)


# --------------------------------------------------------------------------- #
# Rate limiter — fake clock, NO real sleeps                                   #
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.now += dt  # sleeping advances the fake clock


def test_token_bucket_spaces_calls_with_fake_clock():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=1.0, time_func=clock.time, sleep_func=clock.sleep)

    # First acquire: token available, no sleep.
    bucket.acquire()
    assert clock.sleeps == []

    # Next two immediate acquires must each wait ~1s (rate = 1/s).
    bucket.acquire()
    bucket.acquire()
    assert len(clock.sleeps) == 2
    assert all(abs(s - 1.0) < 1e-9 for s in clock.sleeps)
    # Total virtual time elapsed ~= 2s for 2 forced waits.
    assert abs(clock.now - 2.0) < 1e-9


def test_token_bucket_higher_rate_waits_less():
    clock = FakeClock()
    bucket = TokenBucket(rate=4.0, capacity=1.0, time_func=clock.time, sleep_func=clock.sleep)
    bucket.acquire()  # free
    bucket.acquire()  # must wait 1/4 s
    assert len(clock.sleeps) == 1
    assert abs(clock.sleeps[0] - 0.25) < 1e-9


# --------------------------------------------------------------------------- #
# get_json — retries + typed exceptions                                       #
# --------------------------------------------------------------------------- #
URL = "https://example.test/api/thing"


@responses.activate
def test_get_json_happy():
    responses.add(responses.GET, URL, json={"ok": 1}, status=200)
    assert get_json(URL) == {"ok": 1}


@responses.activate
def test_429_then_success_retries():
    responses.add(responses.GET, URL, json={"e": "rate"}, status=429)
    responses.add(responses.GET, URL, json={"e": "rate"}, status=429)
    responses.add(responses.GET, URL, json={"ok": 1}, status=200)
    assert get_json(URL) == {"ok": 1}
    assert len(responses.calls) == 3


@responses.activate
def test_429_exhausted_raises_rate_limited():
    for _ in range(5):
        responses.add(responses.GET, URL, json={"e": "rate"}, status=429)
    with pytest.raises(RateLimited):
        get_json(URL, max_retries=3)
    assert len(responses.calls) == 4  # 1 + 3 retries


@responses.activate
def test_5xx_exhausted_raises_provider_down():
    for _ in range(5):
        responses.add(responses.GET, URL, json={"e": "boom"}, status=503)
    with pytest.raises(ProviderDown):
        get_json(URL, max_retries=3)
    assert len(responses.calls) == 4


@responses.activate
def test_403_raises_blocked_immediately_no_retry():
    responses.add(responses.GET, URL, json={"e": "no"}, status=403)
    with pytest.raises(Blocked) as ei:
        get_json(URL)
    assert ei.value.status == 403
    assert len(responses.calls) == 1


@responses.activate
def test_200_non_json_raises_schema_error():
    from flight_deals.http import SchemaError

    responses.add(responses.GET, URL, body="<html>not json</html>", status=200)
    with pytest.raises(SchemaError):
        get_json(URL)


def test_session_is_per_thread():
    sessions = {}

    def grab(name):
        sessions[name] = http._session()

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    # Different threads get different Session objects (Sessions aren't
    # documented thread-safe, so we never share one).
    assert sessions["a"] is not sessions["b"]
    # Same thread reuses its session.
    assert http._session() is http._session()


def test_user_agents_present():
    assert 2 <= len(http.USER_AGENTS) <= 3
    assert all("Mozilla/5.0" in ua for ua in http.USER_AGENTS)
