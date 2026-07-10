"""
Unit test for scripts/capture_fixtures.py's HTTP-failure handling.

scripts/capture_fixtures.py is a manual, one-shot recorder (Global
Constraint 10: never run by the test suite, never hits the network from
here — see its module docstring). This test imports its pure functions
directly (via importlib, since scripts/ is not a package and is not on
`testpaths`) and drives them against a `responses`-mocked HTTP call, so it
never touches the network and is never collected as part of running the
script itself.

The bug this guards against: a 403/429 (or any >=400) HTTP response does
NOT raise `requests.RequestException`, so a bare `except
requests.RequestException` never sees it — the response would otherwise be
written out as `_captured_live: true` even though it's a blocked/failed
capture. `evaluate_response` (and the capture_* functions that call it)
must treat any unexpected >=400 status as a capture failure and write a
synthetic placeholder instead.
"""

import importlib.util
import json
import sys
from pathlib import Path

import responses

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "capture_fixtures.py"


def _load_capture_fixtures_module():
    spec = importlib.util.spec_from_file_location("capture_fixtures", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture_fixtures = _load_capture_fixtures_module()


@responses.activate
def test_403_during_capture_produces_synthetic_marked_file(tmp_path):
    responses.add(
        responses.GET,
        capture_fixtures.FARFND_ROUNDTRIP_URL,
        json={"message": "Forbidden"},
        status=403,
    )

    cap = capture_fixtures.Capture(sleep_seconds=0.0)
    result = capture_fixtures.capture_farfnd_roundtrip_exact(cap, tmp_path)

    out_file = tmp_path / "farfnd_roundtrip_exact_bud_cfu.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))

    # Never mislabeled as a live capture.
    assert data.get("_captured_live") is not True
    # Always marked synthetic, with the real status + a body excerpt.
    assert data["_synthetic"] is True
    assert data["_capture_failure"]["status_code"] == 403
    assert "Forbidden" in data["_capture_failure"]["body_excerpt"]

    assert result["status"] == "synthetic"
    assert "403" in result["reason"]


@responses.activate
def test_expected_404_still_counts_as_live_capture(tmp_path):
    """The one deliberate exception: wizz_wrong_version_404 WANTS a 404, so
    a matching status is a successful, explicitly-marked live capture."""
    responses.add(
        responses.POST,
        capture_fixtures.WIZZ_TIMETABLE_URL.format(
            version=capture_fixtures.WIZZ_FALLBACK_VERSION_KNOWN_WRONG
        ),
        body="<html>404 - not found</html>",
        status=404,
    )

    cap = capture_fixtures.Capture(sleep_seconds=0.0)
    result = capture_fixtures.capture_wizz_wrong_version_404(cap, tmp_path)

    out_file = tmp_path / "wizz_wrong_version_404.json"
    data = json.loads(out_file.read_text(encoding="utf-8"))

    assert data["_captured_live"] is True
    assert data["_expected_status"] == 404
    assert data["_status_code"] == 404
    assert result["status"] == "captured"
