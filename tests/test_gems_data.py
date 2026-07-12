"""Task 15a — gem-island catalog transcription (data-only validation).

Validates the new ``gems`` array in ``data/destinations.json`` directly
against the JSON file (Global Constraint 10: fixtures/data-only, no live
calls). The registry code does NOT load ``gems`` yet -- that is Task 15b's
engine work -- so these tests read the JSON file directly rather than going
through ``DestinationRegistry``.

Catalog source: docs/research/GEM-CATALOG.md (researched 2026-07-12).

Region KEEP counts asserted below reflect a literal read of the catalog's
per-row Verdict column (not the catalog's own section-header tallies, which
contain an internal arithmetic slip in the Italy+Malta section -- see the
Task 15a report for the reconciliation). Literal row counts: Greece 35 KEEP,
Italy+Malta 17 KEEP, Spain/Croatia/Portugal/other 16 KEEP = 68 KEEP total;
21 MARGINAL total (89 entries transcribed, matching the catalog's own total
count of 89 assessed-and-kept-or-marginal rows).
"""

import json
import re
from pathlib import Path

import pytest

DATA_PATH = Path(__file__).parent.parent / "data" / "destinations.json"

VALID_MODES = {"bus", "train", "taxi", "ferry", "shuttle"}
VALID_MONTHS = {
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
}
SEASON_RE = re.compile(r"^[a-z]{3}-[a-z]{3}$")

COUNTRY_TO_REGION = {
    "Greece": "greece",
    "Italy": "italy_malta",
    "Malta": "italy_malta",
    "Spain": "other",
    "Croatia": "other",
    "Portugal": "other",
    "Bulgaria": "other",
}


@pytest.fixture(scope="module")
def data():
    with open(DATA_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def gems(data):
    return data["gems"]


@pytest.fixture(scope="module")
def registry_airport_codes(data):
    return {a["iata"] for a in data["airports"]}


@pytest.fixture(scope="module")
def taxonomy_tags():
    """Import the live taxonomy from the registry module (not a copy)."""
    from flight_deals.registry.destinations import TERRAIN_TAGS, VIBE_TAGS
    return TERRAIN_TAGS | VIBE_TAGS


# --------------------------------------------------------------------------- #
# structural presence                                                         #
# --------------------------------------------------------------------------- #

def test_gems_key_present_schema_version_unchanged(data):
    assert "gems" in data
    assert isinstance(data["gems"], list)
    # additive change per Design ruling -- schema_version stays put
    assert data["schema_version"] == 2


def test_gem_count_matches_catalog_literal_tally(gems):
    """67+22 is the catalog's own header tally; a literal per-row read of the
    Italy+Malta section's Verdict column yields 17 KEEP/5 MARGINAL rather than
    the header's 16/6 (an internal arithmetic slip -- see module docstring).
    Total assessed-and-transcribed rows (89) matches either way."""
    assert len(gems) == 89


# --------------------------------------------------------------------------- #
# per-region KEEP counts (Task 15a acceptance)                                #
# --------------------------------------------------------------------------- #

def test_keep_counts_per_region(gems):
    keep = [g for g in gems if not g.get("marginal")]
    by_region = {"greece": 0, "italy_malta": 0, "other": 0}
    for g in keep:
        region = COUNTRY_TO_REGION[g["country"]]
        by_region[region] += 1

    assert by_region["greece"] == 35
    assert by_region["italy_malta"] == 17  # literal catalog count; see docstring
    assert by_region["other"] == 16
    assert sum(by_region.values()) == 68


def test_marginal_count_total(gems):
    marginal = [g for g in gems if g.get("marginal")]
    assert len(marginal) == 21


# --------------------------------------------------------------------------- #
# per-gem structural validation                                               #
# --------------------------------------------------------------------------- #

def test_slugs_unique_and_kebab_case(gems):
    slugs = [g["slug"] for g in gems]
    assert len(slugs) == len(set(slugs)), "duplicate slug(s) found"
    kebab_re = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
    for slug in slugs:
        assert kebab_re.match(slug), f"slug not kebab-case: {slug!r}"


def test_required_top_level_fields(gems):
    for g in gems:
        for field in ("slug", "name", "country", "tags", "gateways"):
            assert field in g, f"{g.get('slug', '?')} missing field {field!r}"
        assert isinstance(g["tags"], list) and g["tags"], f"{g['slug']}: empty tags"
        assert isinstance(g["gateways"], list) and g["gateways"], (
            f"{g['slug']}: gateways must be non-empty"
        )


def test_marginal_field_is_bool_true_when_present(gems):
    for g in gems:
        if "marginal" in g:
            assert g["marginal"] is True, f"{g['slug']}: marginal must be True if present"


def test_tags_are_all_in_registry_taxonomy(gems, taxonomy_tags):
    for g in gems:
        for tag in g["tags"]:
            assert tag in taxonomy_tags, f"{g['slug']}: tag {tag!r} not in taxonomy"


def test_gateway_airports_exist_in_registry(gems, registry_airport_codes):
    for g in gems:
        for gw in g["gateways"]:
            assert gw["airport"] in registry_airport_codes, (
                f"{g['slug']}: gateway airport {gw['airport']!r} not in registry"
            )


def test_legs_non_empty_and_modes_valid(gems):
    for g in gems:
        for gw in g["gateways"]:
            assert gw["legs"], f"{g['slug']}/{gw['airport']}: legs must be non-empty"
            for leg in gw["legs"]:
                assert leg["mode"] in VALID_MODES, (
                    f"{g['slug']}/{gw['airport']}: invalid mode {leg['mode']!r}"
                )
                for field in ("from", "to", "minutes", "cost_eur"):
                    assert field in leg, f"{g['slug']}/{gw['airport']}: leg missing {field!r}"


def test_minutes_and_costs_are_positive(gems):
    for g in gems:
        for gw in g["gateways"]:
            assert gw["total_minutes"] > 0, f"{g['slug']}/{gw['airport']}: total_minutes"
            assert gw["total_cost_eur"] > 0, f"{g['slug']}/{gw['airport']}: total_cost_eur"
            for leg in gw["legs"]:
                assert leg["minutes"] > 0, f"{g['slug']}/{gw['airport']}: leg minutes"
                assert leg["cost_eur"] > 0, f"{g['slug']}/{gw['airport']}: leg cost_eur"


def test_gateways_have_notes(gems):
    for g in gems:
        for gw in g["gateways"]:
            assert gw.get("note"), f"{g['slug']}/{gw['airport']}: note must be non-empty"


def test_season_strings_parse_as_month_ranges(gems):
    def check(season, where):
        assert SEASON_RE.match(season), f"{where}: season {season!r} doesn't match ^[a-z]{{3}}-[a-z]{{3}}$"
        lo, hi = season.split("-")
        assert lo in VALID_MONTHS, f"{where}: season {season!r} has invalid start month"
        assert hi in VALID_MONTHS, f"{where}: season {season!r} has invalid end month"

    for g in gems:
        if "season" in g:
            check(g["season"], g["slug"])
        for gw in g["gateways"]:
            if "season" in gw:
                check(gw["season"], f"{g['slug']}/{gw['airport']}")


# --------------------------------------------------------------------------- #
# spot-checks: multi-gateway gems and bundling notes (Task 15a rulings)       #
# --------------------------------------------------------------------------- #

def test_milos_has_both_gateways():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    milos = next(g for g in gems if g["slug"] == "milos")
    airports = {gw["airport"] for gw in milos["gateways"]}
    assert airports == {"ATH", "JTR"}
    jtr_gw = next(gw for gw in milos["gateways"] if gw["airport"] == "JTR")
    assert jtr_gw.get("season") == "jun-sep"


def test_korcula_has_both_gateways_with_differing_seasons():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    korcula = next(g for g in gems if g["slug"] == "korcula")
    airports = {gw["airport"] for gw in korcula["gateways"]}
    assert airports == {"DBV", "SPU"}
    dbv_gw = next(gw for gw in korcula["gateways"] if gw["airport"] == "DBV")
    spu_gw = next(gw for gw in korcula["gateways"] if gw["airport"] == "SPU")
    assert dbv_gw.get("season") == "may-oct"
    assert spu_gw.get("season") == "apr-oct"


def test_stromboli_is_suf_only():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    stromboli = next(g for g in gems if g["slug"] == "stromboli")
    airports = {gw["airport"] for gw in stromboli["gateways"]}
    assert airports == {"SUF"}
    assert stromboli.get("season") == "jun-sep"


def test_ponza_has_nap_and_fco_style_multi_leg_chain():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    ponza = next(g for g in gems if g["slug"] == "ponza")
    airports = {gw["airport"] for gw in ponza["gateways"]}
    assert "NAP" in airports


def test_la_maddalena_notes_caprera_bundle():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    lm = next(g for g in gems if g["slug"] == "la-maddalena")
    note_text = " ".join(gw["note"] for gw in lm["gateways"])
    assert "Caprera" in note_text


def test_sipan_notes_kolocep_lopud_bundle():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    sipan = next(g for g in gems if g["slug"] == "sipan")
    note_text = " ".join(gw["note"] for gw in sipan["gateways"])
    assert "Kolocep" in note_text and "Lopud" in note_text


def test_koufonisia_notes_iraklia_schinoussa_bundle():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    kouf = next(g for g in gems if g["slug"] == "koufonisia")
    note_text = " ".join(gw["note"] for gw in kouf["gateways"])
    assert "Iraklia" in note_text and "Schinoussa" in note_text


def test_asinara_preserves_no_lodging_caveat():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    asinara = next(g for g in gems if g["slug"] == "asinara")
    note_text = " ".join(gw["note"] for gw in asinara["gateways"])
    assert "lodging" in note_text.lower()


def test_kea_preserves_taxi_dominated_cost_caveat():
    with open(DATA_PATH) as f:
        gems = json.load(f)["gems"]
    kea = next(g for g in gems if g["slug"] == "kea")
    note_text = " ".join(gw["note"] for gw in kea["gateways"])
    assert "taxi" in note_text.lower() and "cost" in note_text.lower()


def test_no_drops_present(gems):
    """DROPs (e.g. Alonissos, Skiathos, Gavdos, Filicudi/Alicudi, Capraia,
    Lampedusa/Linosa, Sazan, Varna) must not appear."""
    slugs = {g["slug"] for g in gems}
    dropped = {
        "alonissos", "skiathos", "gavdos", "filicudi", "alicudi",
        "capraia", "lampedusa", "linosa", "sazan", "varna", "corsica",
    }
    assert not (slugs & dropped)
