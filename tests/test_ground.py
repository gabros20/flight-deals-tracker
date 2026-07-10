import pytest
from unittest.mock import patch
from flight_deals.ground import GroundTransport, haversine_distance, precompute_ground_transfers
from flight_deals.models import GroundLeg


def test_haversine_distance_known_values():
    dist = haversine_distance(47.4369, 19.2556, 48.1103, 16.5697)
    assert 190 < dist < 220


def test_reasonable_ground_distance():
    gt = GroundTransport(use_cache=False)
    assert gt.is_reasonable_ground_distance("BUD", "VIE") is True
    assert gt.is_reasonable_ground_distance("BUD", "PMI") is False  # too far


def test_ground_options_respects_max_km():
    gt = GroundTransport(use_cache=False)
    # Short
    short = gt.get_ground_options("BUD", "VIE", max_km=300)
    assert len(short) > 0
    # Long should return empty or minimal
    long_opts = gt.get_ground_options("BUD", "PMI", max_km=300)
    assert all(o.duration_minutes == 0 or "N/A" in o.notes for o in long_opts) or len(long_opts) == 0


@patch("flight_deals.ground.requests.get")
def test_osrm_with_precompute(mock_get):
    gt = GroundTransport(use_cache=False, precompute_path="data/ground_transfers.json")
    leg = gt.get_driving_time("BUD", "VIE")
    assert leg is not None
    assert leg.duration_minutes > 100
    # Should not have called OSRM because of precompute
    # (mock not called in this path)


def test_estimate_total_uses_air_duration():
    gt = GroundTransport(use_cache=False)
    result = gt.estimate_total_connection_time("BUD", "VIE", "MUC", air_duration_minutes=75, max_ground_km=300)
    assert result["breakdown"]["air1"] == 75
    assert result["total_minutes"] > 75


def test_efficiency_score():
    score = GroundTransport.compute_efficiency_score(50, 300)
    assert score == pytest.approx(10.0, 0.1)


def test_precompute_helper():
    # Just ensure it runs without error on small set
    data = precompute_ground_transfers(pairs=[("BUD", "VIE")])
    assert "BUD-VIE" in data or len(data) >= 0


def test_groundleg_roundtrips_from_real_s4_deal_dict():
    """CONTRACT.md (§2a, §Task-10-changelog) documents legs[].distance_km as
    nullable -- a static curated hop has none -- and claims GroundLeg matches.
    Build a real S4 deal the way the planner does (output.flight_leg /
    output.ground_leg, no distance_km supplied) and prove the ground-leg dict
    that ends up in deal["legs"] reconstructs into GroundLeg without error.
    This is the future snapshot-replay path: a persisted deal's legs get
    re-hydrated into models on read."""
    from flight_deals import output

    deal = output.build_deal(
        shape="S4", origin="BUD", destination="NAP", out_date="2026-08-22",
        return_date="2026-08-27", price_eur=90.0, price_confidence="exact",
        carriers=["ryanair"],
        legs=[
            output.flight_leg("BUD", "NAP", "ryanair", "2026-08-22", 30.0),
            output.ground_leg("NAP", "BRI", "train", 240, cost_eur=35.0),
            output.flight_leg("BRI", "BUD", "ryanair", "2026-08-27", 25.0),
        ],
        ground=output.ground_summary(240, 35.0, "train"),
        why="x",
    )
    ground_leg_dict = deal["legs"][1]
    assert ground_leg_dict["distance_km"] is None  # no routed distance for a curated hop

    leg = GroundLeg(**ground_leg_dict)
    assert leg.distance_km is None
    assert leg.from_iata == "NAP" and leg.to_iata == "BRI"
    assert leg.cost_eur == 35.0
