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
def _fast_http(monkeypatch):
    """
    Keep the shared rate limiter from actually spacing calls during tests, make
    retry backoff instant, and stub Wizz's version-discovery so constructing a
    DealOrchestrator (which builds a WizzProvider) never touches the network.
    Tests that assert on rate-limiter *spacing* build their own TokenBucket with
    an injected fake clock, so they are unaffected by the rate override.
    """
    from flight_deals.providers.wizz import WizzProvider

    http.set_rate(1_000_000.0)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(WizzProvider, "_get_current_version",
                        lambda self: WizzProvider.FALLBACK_VERSION)
    yield
    http.set_rate(1.0)
