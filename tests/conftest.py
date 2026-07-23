import json
import time
from pathlib import Path

import pytest

from flight_deals import http

FIXTURES = Path(__file__).parent / "fixtures"


def load_body(name: str):
    """Load a captured fixture and return its raw provider `body` (what the
    live endpoint returns, without the capture wrapper)."""
    data = json.loads((FIXTURES / name).read_text())
    return data["body"]


@pytest.fixture(autouse=True)
def _fast_http(monkeypatch, tmp_path):
    """
    Keep the shared rate limiter from actually spacing calls during tests and
    make retry backoff instant. Tests that assert on rate-limiter *spacing*
    build their own TokenBucket with an injected fake clock, so they are
    unaffected by the rate override.

    The rebuilt Wizz provider (Task 4) does NO network I/O at construction — the
    API version is resolved lazily from data/wizz_version.txt (offline) and only
    re-discovered on a drift 404 — so no version-sniff stub is needed. We reset
    the module-level version cache so a discovery in one test can't leak a
    version into the next, AND point the version-file path at a fresh temp path
    that doesn't exist, so resolution deterministically falls through to
    ``WizzProvider.FALLBACK_VERSION``. Without that isolation the suite reads the
    real, committed ``data/wizz_version.txt`` — which auto-advances in production
    on every version drift — and every test that mocks the FALLBACK_VERSION URL
    breaks the moment that file moves off FALLBACK (a route-not-served / drift
    bug this same change fixes on the wire). Tests that need their own version
    file (e.g. concurrent-discovery) re-patch ``resolve_path`` themselves; that
    later monkeypatch wins.
    """
    from flight_deals.providers import wizz as wizz_mod
    from flight_deals import fx

    http.set_rate(1_000_000.0)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(wizz_mod, "resolve_path", lambda _p: tmp_path / "no_wizz_version.txt")
    wizz_mod.reset_version_cache()
    # Force each test to (re)load the fx table lazily from the real committed
    # file, so a test that swaps in a stale/partial table can't pollute another.
    fx._TABLE._loaded = False
    yield
    wizz_mod.reset_version_cache()
    fx._TABLE._loaded = False
    http.set_rate(1.0)
