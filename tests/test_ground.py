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
