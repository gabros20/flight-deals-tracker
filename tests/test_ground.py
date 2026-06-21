import pytest
from unittest.mock import patch, MagicMock
from flight_deals.ground import GroundTransport, haversine_distance
from flight_deals.models import GroundLeg


def test_haversine_distance_known_values():
    # BUD to VIE approx distance ~200km
    dist = haversine_distance(47.4369, 19.2556, 48.1103, 16.5697)
    assert 190 < dist < 220


def test_ground_transport_driving_fallback():
    gt = GroundTransport(use_cache=False)
    with patch("flight_deals.ground.requests.get") as mock_get:
        mock_get.side_effect = Exception("simulated failure")
        with patch.object(gt, "_get_airport_coords", return_value=(47.4369, 19.2556)):
            leg = gt.get_driving_time("BUD", "VIE")
            assert isinstance(leg, GroundLeg)
            assert leg.duration_minutes > 0
            assert leg.mode == "driving"


@patch("flight_deals.ground.requests.get")
def test_ground_transport_osrm_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "code": "Ok",
        "routes": [{"duration": 7200, "distance": 210000}]
    }
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    gt = GroundTransport(use_cache=False)
    with patch.object(gt, "_get_airport_coords", side_effect=[(47.4369, 19.2556), (48.1103, 16.5697)]):
        leg = gt.get_driving_time("BUD", "VIE")
        assert leg is not None
        assert leg.duration_minutes == 120
        assert leg.distance_km == 210.0


def test_ground_options_returns_list():
    gt = GroundTransport(use_cache=False)
    with patch.object(gt, "_get_airport_coords", return_value=(47.4369, 19.2556)):
        options = gt.get_ground_options("BUD", "VIE")
        assert isinstance(options, list)
        assert len(options) >= 1
        for o in options:
            assert isinstance(o, GroundLeg)


def test_estimate_total_connection_time():
    gt = GroundTransport(use_cache=False)
    with patch.object(gt, "get_driving_time") as mock_drive:
        mock_drive.return_value = GroundLeg(from_iata="BUD", to_iata="VIE", mode="driving", duration_minutes=45, distance_km=200)
        result = gt.estimate_total_connection_time("BUD", "VIE", "PMI", flight1_min=90, flight2_min=120)
        assert result["total_minutes"] > 200
        assert "breakdown" in result
