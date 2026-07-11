"""output.py — the single renderer: deal_id golden vector, envelope invariants,
render/telegram paths."""

import hashlib

from flight_deals import output


# --- deal_id (frozen, CONTRACT §5) — golden vector ------------------------- #
def test_deal_id_golden_vector():
    """Pins the frozen derivation to the exact value in docs/CONTRACT.md §2."""
    assert output.deal_id("BUD", "CFU", "2026-08-22", "2026-08-27", "S2", ["ryanair"]) == "a48e258b18"


def test_deal_id_one_way_uses_empty_string_not_none():
    manual = hashlib.sha256("BUD|CFU|2026-08-22||S1|ryanair".encode()).hexdigest()[:10]
    assert output.deal_id("BUD", "CFU", "2026-08-22", None, "S1", ["ryanair"]) == manual


def test_deal_id_carrier_order_independent():
    a = output.deal_id("BUD", "VIE", "2026-08-22", "2026-08-27", "S5", ["ryanair", "wizzair"])
    b = output.deal_id("BUD", "VIE", "2026-08-22", "2026-08-27", "S5", ["wizzair", "ryanair"])
    assert a == b


# --- build_deal ------------------------------------------------------------ #
def test_build_deal_computes_nights_and_sorts_carriers():
    d = output.build_deal(
        shape="S2", origin="BUD", destination="CFU", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=89.98, price_confidence="exact",
        carriers=["wizzair", "ryanair"], legs=[], why="x",
    )
    assert d["nights"] == 5
    assert d["carriers"] == ["ryanair", "wizzair"]  # sorted
    assert set(d["links"]) == {"ryanair", "wizzair"}


# --- envelope invariants --------------------------------------------------- #
def test_envelope_attaches_route_status_only_when_empty():
    non_empty = output.envelope([{"x": 1}], "s", {"ryanair": "ok"}, [], route_status="no_service")
    assert "route_status" not in non_empty

    empty = output.envelope([], "s", {"ryanair": "ok"}, [], route_status="no_service")
    assert empty["route_status"] == "no_service"


def test_error_envelope_pairs_error_and_hint():
    env = output.error_envelope("invalid_spec", "fix it like so")
    assert env["error"] == "invalid_spec" and env["hint"] == "fix it like so"
    assert env["results"] == []


def test_project_sources_flattens_to_status_strings():
    agg = {"ryanair": {"ok": True, "status": "ok"}, "wizzair": {"ok": False, "status": "blocked"}}
    assert output.project_sources(agg) == {"ryanair": "ok", "wizzair": "blocked"}


def test_render_json_is_parseable_and_pretty_is_text():
    import json
    env = output.envelope([], "no service", {"ryanair": "ok"}, [], route_status="no_service")
    assert json.loads(output.render(env, pretty=False))["summary"] == "no service"
    assert "no service" in output.render(env, pretty=True)
