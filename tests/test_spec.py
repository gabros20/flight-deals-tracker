"""SearchSpec + the depart/nights DSL (engine/spec.py)."""

import pytest

from flight_deals.engine.spec import (
    SpecError,
    parse_depart,
    parse_nights,
    parse_spec,
)


# --- depart DSL ------------------------------------------------------------ #
def test_depart_single_date():
    d = parse_depart("2026-08-22")
    assert d.kind == "dates" and d.out_from == d.out_to == "2026-08-22"


def test_depart_window():
    d = parse_depart("2026-08-22..2026-08-24")
    assert d.kind == "window" and d.out_from == "2026-08-22" and d.out_to == "2026-08-24"


def test_depart_month_resolves_to_full_month():
    d = parse_depart("2026-08")
    assert d.kind == "month" and d.out_from == "2026-08-01" and d.out_to == "2026-08-31"
    assert d.month == "2026-08"


def test_depart_february_month_end():
    assert parse_depart("2026-02").out_to == "2026-02-28"


def test_depart_comma_list_sorted_deduped():
    d = parse_depart("2026-08-29, 2026-08-22, 2026-08-22")
    assert d.kind == "dates" and d.dates == ["2026-08-22", "2026-08-29"]
    assert d.out_from == "2026-08-22" and d.out_to == "2026-08-29"


def test_depart_reversed_window_hint():
    with pytest.raises(SpecError) as ei:
        parse_depart("2026-08-24..2026-08-22")
    assert "2026-08-22..2026-08-24" in ei.value.hint


def test_depart_garbage_has_hint():
    with pytest.raises(SpecError) as ei:
        parse_depart("next tuesday")
    assert ei.value.hint


# --- nights DSL ------------------------------------------------------------ #
def test_nights_single_and_range():
    assert parse_nights("5") == (5, 5)
    assert parse_nights("5-8") == (5, 8)


def test_nights_none_is_one_way():
    assert parse_nights(None) is None


def test_nights_reversed_range_hint():
    with pytest.raises(SpecError) as ei:
        parse_nights("8-5")
    assert ei.value.hint == 'try "5-8"'


# --- SearchSpec ------------------------------------------------------------ #
def test_spec_defaults():
    s = parse_spec({"depart": "2026-08-22..2026-08-24", "nights": "5-8"})
    assert s.origins == ["BUD"]
    assert s.carriers == ["ryanair", "wizzair"]
    assert s.shapes == ["direct"]
    assert s.max_results == 10
    assert s.is_round_trip


def test_spec_accepts_all_shapes_forward_compatible():
    # The schema accepts all four; the planner is what refuses the disabled ones.
    s = parse_spec({"depart": "2026-08", "nights": "3-5",
                    "shapes": ["direct", "extended-origin", "open-jaw", "via-hub"]})
    assert s.shapes == ["direct", "extended-origin", "open-jaw", "via-hub"]


def test_spec_origins_uppercased_and_validated():
    s = parse_spec({"origins": ["bud", "vie"], "depart": "2026-08", "nights": "3"})
    assert s.origins == ["BUD", "VIE"]


def test_spec_bad_iata_exits_with_hint():
    with pytest.raises(SpecError) as ei:
        parse_spec({"origins": ["BUDA"], "depart": "2026-08", "nights": "3"})
    assert ei.value.hint


def test_spec_unknown_field_rejected_with_hint():
    with pytest.raises(SpecError) as ei:
        parse_spec({"depart": "2026-08", "nights": "3", "wheer": "seaside"})
    assert "Example spec" in ei.value.hint


def test_spec_bad_depart_propagates_hint():
    with pytest.raises(SpecError) as ei:
        parse_spec({"depart": "soon", "nights": "3"})
    assert ei.value.hint


def test_spec_unwraps_saved_search_wrapper():
    s = parse_spec({"name": "x", "spec": {"depart": "2026-08", "nights": "3", "where": "seaside"}})
    assert s.where == "seaside"
