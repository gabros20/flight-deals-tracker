"""
Validates docs/CONTRACT.md against:

1. The live/synthetic fixtures captured by scripts/capture_fixtures.py
   (tests/fixtures/) — do they parse, and do they match the raw provider
   shapes CONTRACT.md documents as the basis for the future Deal envelope?
2. Hand-built example envelopes/Deals, checked with lightweight,
   by-hand "jsonschema" validators (no new dependency) that encode the
   rules CONTRACT.md declares. This is deliberately NOT a test of CLI
   output — the envelope isn't implemented yet (that's Task 6); these
   validators exist so Task 6+ can import/adapt them instead of
   re-deriving the rules from prose.

Nothing here hits the network — Global Constraint 10.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

EXPECTED_JSON_FIXTURES = [
    "farfnd_roundtrip_exact_bud_cfu.json",
    "farfnd_roundtrip_anywhere_bud.json",
    "farfnd_cheapest_per_day_bud_cfu.json",
    "farfnd_cheapest_per_day_cfu_bud.json",
    "farfnd_roundtrip_empty_nonexistent.json",
    "wizz_timetable_bud_cta.json",
    "wizz_wrong_version_404.json",
]
EXPECTED_HTML_FIXTURES = [
    "wizz_version_discovery_snippet.html",
]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Fixtures exist, parse, and are internally consistent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", EXPECTED_JSON_FIXTURES)
def test_fixture_file_exists_and_parses(name):
    path = FIXTURES_DIR / name
    assert path.exists(), f"missing fixture {name} — run scripts/capture_fixtures.py"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


@pytest.mark.parametrize("name", EXPECTED_JSON_FIXTURES)
def test_fixture_marks_capture_provenance(name):
    """Exactly one of _captured_live / _synthetic must be true, per the
    brief's capture-or-mark-synthetic contract."""
    data = load_fixture(name)
    live = data.get("_captured_live") is True
    synthetic = data.get("_synthetic") is True
    assert live != synthetic, f"{name}: must be exactly one of live/synthetic, got live={live} synthetic={synthetic}"
    if synthetic:
        assert data.get("_synthetic_reason"), f"{name}: synthetic fixture must document why"
    assert "body" in data


@pytest.mark.parametrize("name", EXPECTED_HTML_FIXTURES)
def test_html_fixture_exists_and_has_version_pattern(name):
    path = FIXTURES_DIR / name
    assert path.exists(), f"missing fixture {name} — run scripts/capture_fixtures.py"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"be\.wizzair\.com/\d+\.\d+\.\d+", text), (
        f"{name}: expected a be.wizzair.com/X.Y.Z version string "
        "(live capture) or the documented synthetic placeholder"
    )


def test_farfnd_roundtrip_exact_shape():
    data = load_fixture("farfnd_roundtrip_exact_bud_cfu.json")
    fares = data["body"]["fares"]
    assert isinstance(fares, list) and len(fares) >= 1
    fare = fares[0]
    for leg_key in ("outbound", "inbound"):
        assert leg_key in fare, f"exact round-trip fixture missing {leg_key} leg"
        leg = fare[leg_key]
        assert leg["price"]["currencyCode"] == "EUR"
        assert isinstance(leg["price"]["value"], (int, float))
        assert "iataCode" in leg["departureAirport"]
        assert "iataCode" in leg["arrivalAirport"]
    assert fare["outbound"]["departureAirport"]["iataCode"] == "BUD"
    assert fare["outbound"]["arrivalAirport"]["iataCode"] == "CFU"


def test_farfnd_roundtrip_anywhere_shape():
    data = load_fixture("farfnd_roundtrip_anywhere_bud.json")
    body = data["body"]
    fares = body["fares"]
    assert isinstance(fares, list)
    assert len(fares) <= 20, "anywhere-mode responses must be truncated to <=20 entries"
    if len(fares) == 20:
        assert body.get("_truncated") is True
    for fare in fares:
        assert fare["outbound"]["departureAirport"]["iataCode"] == "BUD"
        # anywhere-mode: destination varies per fare, unlike the exact fixture
        assert "iataCode" in fare["outbound"]["arrivalAirport"]


@pytest.mark.parametrize("name", [
    "farfnd_cheapest_per_day_bud_cfu.json",
    "farfnd_cheapest_per_day_cfu_bud.json",
])
def test_farfnd_cheapest_per_day_shape(name):
    data = load_fixture(name)
    outbound = data["body"]["outbound"]
    fares = outbound["fares"]
    assert isinstance(fares, list) and len(fares) >= 1
    assert len(fares) <= 20, "cheapestPerDay must be truncated to <=20 entries"
    if len(fares) == 20:
        assert outbound.get("_truncated") is True
    for day_entry in fares:
        assert "day" in day_entry
        assert "unavailable" in day_entry
        # invariant: a day is either priced or explicitly unavailable, never both/neither
        if day_entry["unavailable"]:
            assert day_entry["price"] is None
        else:
            assert day_entry["price"] is not None
            assert day_entry["price"]["currencyCode"] == "EUR"


def test_farfnd_empty_nonexistent_route_is_200_with_empty_fares():
    data = load_fixture("farfnd_roundtrip_empty_nonexistent.json")
    assert data["_status_code"] == 200
    assert data["body"]["fares"] == []


def test_wizz_timetable_has_both_directions():
    data = load_fixture("wizz_timetable_bud_cta.json")
    body = data["body"]
    assert "outboundFlights" in body and "returnFlights" in body
    assert len(body["outboundFlights"]) >= 1
    assert len(body["returnFlights"]) >= 1
    for direction in ("outboundFlights", "returnFlights"):
        assert len(body[direction]) <= 20
        for flight in body[direction]:
            assert "price" in flight and "amount" in flight["price"]
            # Wizz timetable is origin-market currency, NOT necessarily EUR
            # (documented HIGH-priority fix in UPGRADE-PLAN §3 — conversion
            # happens at the provider boundary in fx.py, Task 4, not here).
            assert "currencyCode" in flight["price"]


def test_wizz_wrong_version_is_not_200():
    data = load_fixture("wizz_wrong_version_404.json")
    assert data["_status_code"] != 200, "the deliberately-wrong version must not succeed"


# ---------------------------------------------------------------------------
# 2. Lightweight by-hand validators for the envelope/Deal shapes CONTRACT.md
#    declares (§1, §2, §3, §4, §6). No jsonschema dependency, per the brief.
# ---------------------------------------------------------------------------

VALID_SHAPES = {"S1", "S2", "S3", "S4", "S5"}
VALID_CONFIDENCE = {"exact", "approximate"}
VALID_ROUTE_STATUS = {"no_service", "no_match", "provider_error"}
DEAL_ID_RE = re.compile(r"^[0-9a-f]{10}$")


def validate_deal(deal: dict) -> list[str]:
    errors = []
    required = [
        "deal_id", "shape", "origin", "destination", "out_date", "return_date",
        "nights", "price_eur", "price_confidence", "carriers", "legs", "ground",
        "why", "links",
    ]
    for field in required:
        if field not in deal:
            errors.append(f"deal missing field: {field}")
    if errors:
        return errors  # further checks assume presence

    if not DEAL_ID_RE.match(deal["deal_id"]):
        errors.append(f"deal_id must be 10 lowercase hex chars, got {deal['deal_id']!r}")
    if deal["shape"] not in VALID_SHAPES:
        errors.append(f"invalid shape: {deal['shape']!r}")
    if deal["price_confidence"] not in VALID_CONFIDENCE:
        errors.append(f"invalid price_confidence: {deal['price_confidence']!r}")
    if not isinstance(deal["carriers"], list) or not deal["carriers"]:
        errors.append("carriers must be a non-empty list")
    if not isinstance(deal["legs"], list) or not deal["legs"]:
        errors.append("legs must be a non-empty list")
    if not isinstance(deal["price_eur"], (int, float)):
        errors.append("price_eur must be numeric")

    is_one_way = deal["shape"] == "S1"
    if is_one_way and deal["return_date"] is not None:
        errors.append("S1 (one-way) must have return_date == null")
    if not is_one_way and deal["return_date"] is None:
        errors.append(f"{deal['shape']} (round-trip shape) must have a return_date")
    if is_one_way and deal["nights"] is not None:
        errors.append("S1 (one-way) must have nights == null")
    if not is_one_way and deal["nights"] is None:
        errors.append(f"{deal['shape']} must have nights computed")

    for i, leg in enumerate(deal.get("legs", [])):
        if leg.get("type") == "flight":
            for f in ("origin", "destination", "carrier", "departure_date", "price_eur"):
                if f not in leg:
                    errors.append(f"legs[{i}] (flight) missing {f}")
        elif leg.get("type") == "ground":
            for f in ("from_iata", "to_iata", "mode", "duration_minutes", "distance_km"):
                if f not in leg:
                    errors.append(f"legs[{i}] (ground) missing {f}")
        else:
            errors.append(f"legs[{i}] has invalid/missing type: {leg.get('type')!r}")

    if deal["shape"] in ("S1", "S2") and deal["ground"] is not None:
        errors.append(f"{deal['shape']} must have ground == null")
    if deal["shape"] in ("S3", "S4", "S5") and deal["ground"] is None:
        errors.append(f"{deal['shape']} must have a ground summary")

    return errors


def validate_envelope(env: dict) -> list[str]:
    errors = []
    for field in ("results", "summary", "sources", "next"):
        if field not in env:
            errors.append(f"envelope missing field: {field}")
    if errors:
        return errors

    if not isinstance(env["results"], list):
        errors.append("results must be a list")
    if not isinstance(env["summary"], str) or not env["summary"]:
        errors.append("summary must be a non-empty string")
    if not isinstance(env["sources"], dict):
        errors.append("sources must be a dict")
    if not isinstance(env["next"], list):
        errors.append("next must be a list")

    has_error = "error" in env
    has_hint = "hint" in env
    if has_error != has_hint:
        errors.append("error and hint must appear together or not at all")

    if env["results"] == []:
        if "route_status" not in env:
            errors.append("empty results must carry route_status")
        elif env["route_status"] not in VALID_ROUTE_STATUS:
            errors.append(f"invalid route_status: {env['route_status']!r}")

    for i, deal in enumerate(env.get("results", [])):
        deal_errors = validate_deal(deal)
        errors.extend(f"results[{i}]: {e}" for e in deal_errors)

    return errors


def _example_s2_deal() -> dict:
    """The worked example from docs/CONTRACT.md §2, verbatim."""
    return {
        "deal_id": "a48e258b18",
        "shape": "S2",
        "origin": "BUD",
        "destination": "CFU",
        "out_date": "2026-08-22",
        "return_date": "2026-08-27",
        "nights": 5,
        "price_eur": 89.98,
        "price_confidence": "exact",
        "carriers": ["ryanair"],
        "legs": [
            {
                "type": "flight", "origin": "BUD", "destination": "CFU", "carrier": "ryanair",
                "departure_date": "2026-08-22", "departure_time": "10:35",
                "flight_number": "FR 1234", "price_eur": 44.99, "duration_minutes": 105,
            },
            {
                "type": "flight", "origin": "CFU", "destination": "BUD", "carrier": "ryanair",
                "departure_date": "2026-08-27", "departure_time": "22:10",
                "flight_number": "FR 1235", "price_eur": 44.99, "duration_minutes": 100,
            },
        ],
        "ground": None,
        "why": "€89 vs typical €140 for this route, 36% below, 42 observations",
        "links": {"ryanair": "https://www.ryanair.com/gb/en/trip/flights/select?originIata=BUD&destinationIata=CFU"},
    }


def test_example_deal_from_contract_is_valid():
    assert validate_deal(_example_s2_deal()) == []


def test_example_envelope_is_valid():
    env = {
        "results": [_example_s2_deal()],
        "summary": "1 deal found, cheapest €89.98 BUD->CFU",
        "sources": {"ryanair": "ok"},
        "next": ["flight-deals check a48e258b18"],
    }
    assert validate_envelope(env) == []


def test_empty_results_requires_route_status():
    env = {"results": [], "summary": "no service on this route in the window", "sources": {"ryanair": "ok"}, "next": []}
    errors = validate_envelope(env)
    assert any("route_status" in e for e in errors)

    env["route_status"] = "no_service"
    assert validate_envelope(env) == []

    env["route_status"] = "made_up_status"
    errors = validate_envelope(env)
    assert any("invalid route_status" in e for e in errors)


def test_error_and_hint_must_be_paired():
    env = {"results": [], "summary": "bad input", "sources": {}, "next": [], "route_status": "no_match", "error": "invalid_iata"}
    errors = validate_envelope(env)
    assert any("error and hint" in e for e in errors)

    env["hint"] = "did you mean BUD?"
    assert validate_envelope(env) == []


def test_one_way_deal_must_have_null_return_date_and_nights():
    deal = _example_s2_deal()
    deal["shape"] = "S1"
    # still has return_date/nights set -> invalid for S1
    errors = validate_deal(deal)
    assert any("return_date" in e for e in errors)
    assert any("nights" in e for e in errors)

    deal["return_date"] = None
    deal["nights"] = None
    assert validate_deal(deal) == []


def test_extended_origin_deal_requires_ground_summary():
    deal = _example_s2_deal()
    deal["shape"] = "S3"
    errors = validate_deal(deal)
    assert any("ground summary" in e for e in errors)

    deal["ground"] = {"duration_minutes": 150, "cost_eur": 12.0, "mode": "driving"}
    assert validate_deal(deal) == []


# ---------------------------------------------------------------------------
# 3. deal_id derivation (docs/CONTRACT.md §5), reference implementation
# ---------------------------------------------------------------------------

def deal_id(origin: str, destination: str, out_date: str, return_date, shape: str, carriers: list[str]) -> str:
    key = "|".join([
        origin.upper(),
        destination.upper(),
        out_date,
        return_date or "",
        shape,
        "+".join(sorted(c.lower() for c in carriers)),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


def test_deal_id_is_ten_lowercase_hex_chars():
    result = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"])
    assert DEAL_ID_RE.match(result)


def test_deal_id_excludes_price():
    """The whole point: re-pricing the same trip must not change its id."""
    a = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"])
    b = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"])
    assert a == b  # deterministic; price was never a parameter to begin with


def test_deal_id_is_carrier_order_independent():
    a = deal_id("BUD", "VIE", "2026-08-22", "2026-08-27", "S5", ["ryanair", "wizzair"])
    b = deal_id("BUD", "VIE", "2026-08-22", "2026-08-27", "S5", ["wizzair", "ryanair"])
    assert a == b


def test_deal_id_differs_for_different_shapes_or_dates():
    base = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"])
    different_shape = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S3", ["ryanair"])
    different_date = deal_id("BUD", "CFU", "2026-08-23", "2026-08-27", "S2", ["ryanair"])
    one_way = deal_id("BUD", "CFU", "2026-08-22", None, "S1", ["ryanair"])
    assert len({base, different_shape, different_date, one_way}) == 4


def test_deal_id_golden_vector_matches_contract_worked_example():
    """Pins the exact sha256-derived value for the docs/CONTRACT.md §2
    worked example (BUD|CFU|2026-08-22|2026-08-27|S2|ryanair), so the
    `deal_id` embedded in the doc and in `_example_s2_deal()` above can never
    silently drift from what this reference derivation actually produces.
    Tasks 6/7 must produce this same value for the same inputs."""
    result = deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"])
    assert result == "a48e258b18"
    assert result == _example_s2_deal()["deal_id"]
