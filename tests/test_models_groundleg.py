from flight_deals import output
from flight_deals.models import GroundLeg


def test_groundleg_roundtrips_from_real_s4_deal_dict():
    """CONTRACT.md (§2a, §Task-10-changelog) documents legs[].distance_km as
    nullable -- a static curated hop has none -- and claims GroundLeg matches.
    Build a real S4 deal the way the planner does (output.flight_leg /
    output.ground_leg, no distance_km supplied) and prove the ground-leg dict
    that ends up in deal["legs"] reconstructs into GroundLeg without error.
    This is the future snapshot-replay path: a persisted deal's legs get
    re-hydrated into models on read."""
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
