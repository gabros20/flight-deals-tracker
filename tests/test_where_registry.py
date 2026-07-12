"""Task 5 — where-algebra parser, aliases, matching, and v2 dataset validation."""
import json

import pytest
from typer.testing import CliRunner

from flight_deals.cli import app
from flight_deals.registry.destinations import (
    COUNTRY_TAGS,
    HUB_IATAS,
    REGION_TAGS,
    SEASONAL_TAGS,
    TERRAIN_TAGS,
    VIBE_TAGS,
    DestinationRegistry,
)
from flight_deals.registry.where import Tag, WhereParseError, where_parse

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Parser: precedence, parens, not, errors                                     #
# --------------------------------------------------------------------------- #
def _ev(expr, tags, aliases=None):
    return where_parse(expr, aliases or {}).eval(set(tags))


def test_bare_tag():
    assert _ev("seaside", {"seaside"})
    assert not _ev("seaside", {"island"})


def test_and_or_not():
    assert _ev("a & b", {"a", "b"})
    assert not _ev("a & b", {"a"})
    assert _ev("a | b", {"b"})
    assert _ev("!a", {"b"})
    assert not _ev("!a", {"a"})


def test_precedence_not_over_and_over_or():
    # !a & b | c  ==  ((!a) & b) | c
    assert _ev("!a & b | c", {"c"})            # c alone satisfies the trailing | c
    assert _ev("!a & b | c", {"b"})            # !a true, b true -> left conj true
    assert not _ev("!a & b | c", {"a", "b"})   # a present -> !a false, and no c
    # & binds tighter than |: a | b & c == a | (b & c)
    assert _ev("a | b & c", {"a"})
    assert not _ev("a | b & c", {"b"})
    assert _ev("a | b & c", {"b", "c"})


def test_parens_override_precedence():
    # (a | b) & c
    assert _ev("(a | b) & c", {"a", "c"})
    assert not _ev("(a | b) & c", {"a"})
    assert not _ev("(a | b) & c", {"c"})


def test_double_negation_and_nested_parens():
    assert _ev("!!a", {"a"})
    assert _ev("!(a & b)", {"a"})
    assert not _ev("!(a & b)", {"a", "b"})


@pytest.mark.parametrize("bad", ["", "seaside &", "& seaside", "(seaside", "seaside)", "a | | b", "seaside @ italy"])
def test_bad_expressions_raise_with_hint(bad):
    with pytest.raises(WhereParseError) as exc:
        where_parse(bad, {})
    assert exc.value.hint  # actionable hint always present


def test_case_insensitive_tags():
    assert _ev("Italy", {"italy"})
    assert _ev("SEASIDE & Italy", {"seaside", "italy"})


# --------------------------------------------------------------------------- #
# Adversarial input guards — a pathological expression must raise a clean     #
# WhereParseError (exit-2 path), never a raw RecursionError/traceback.        #
# --------------------------------------------------------------------------- #
def test_deep_nested_parens_raises_clean_error():
    expr = "(" * 600 + "a" + ")" * 600
    with pytest.raises(WhereParseError) as exc:
        where_parse(expr, {})
    assert exc.value.hint


def test_long_not_chain_raises_clean_error():
    expr = "!" * 1500 + "a"
    with pytest.raises(WhereParseError) as exc:
        where_parse(expr, {})
    assert exc.value.hint


def test_token_count_cap_raises_clean_error():
    expr = " | ".join(f"t{i}" for i in range(300))  # ~599 tokens > MAX_TOKENS
    with pytest.raises(WhereParseError) as exc:
        where_parse(expr, {})
    assert exc.value.hint


def test_alias_fanout_cap_raises_clean_error():
    # Each level doubles: a0 -> a1 & a1 -> (a2 & a2) & (a2 & a2) -> ... This
    # would blow past MAX_ALIAS_EXPANSIONS (2**20 >> 10_000) long before the
    # (bounded, ~20-deep) alias-chain recursion could ever threaten the stack.
    aliases = {f"a{i}": f"a{i + 1} & a{i + 1}" for i in range(20)}
    aliases["a20"] = "x"
    with pytest.raises(WhereParseError) as exc:
        where_parse("a0", aliases)
    assert exc.value.hint


# --------------------------------------------------------------------------- #
# Aliases                                                                      #
# --------------------------------------------------------------------------- #
def test_alias_simple_synonym():
    reg = DestinationRegistry()
    italian = {a.iata for a in reg.matching("italian")}
    italy = {a.iata for a in reg.matching("italy")}
    assert italian == italy and italy


def test_alias_compound_expansion():
    reg = DestinationRegistry()
    gi = {a.iata for a in reg.matching("greek-islands")}
    expected = {a.iata for a in reg.matching("island & greece")}
    assert gi == expected
    assert "CFU" in gi and "SKG" not in gi  # Thessaloniki is greek but not an island


def test_alias_backward_compat_v1_names():
    reg = DestinationRegistry()
    assert {a.iata for a in reg.matching("european-islands")} == {a.iata for a in reg.matching("island")}
    assert {a.iata for a in reg.matching("italian-gems")} == {a.iata for a in reg.matching("italy")}


def test_alias_cycle_detected():
    with pytest.raises(WhereParseError):
        where_parse("x", {"x": "y", "y": "x"})


# --------------------------------------------------------------------------- #
# Matching against the real v2 registry                                       #
# --------------------------------------------------------------------------- #
def test_matching_acceptance_query():
    reg = DestinationRegistry()
    got = {a.iata for a in reg.matching("seaside & (italy | spain)")}
    # sane: includes italian + spanish seaside, excludes greek/portuguese
    assert {"CTA", "NAP", "ALC", "PMI"} <= got
    assert "CFU" not in got and "FAO" not in got


def test_matching_negation_excludes_region():
    reg = DestinationRegistry()
    got = {a.iata for a in reg.matching("island & !canaries")}
    assert got and got.isdisjoint({"TFS", "LPA", "ACE", "FUE"})
    assert "PMI" in got  # baleares island survives


def test_hub_is_derived_tag():
    reg = DestinationRegistry()
    hubs = {a.iata for a in reg.matching("hub")}
    assert "VIE" in hubs and "BGY" in hubs
    # not a hand-curated tag on the airport records
    vie = next(a for a in reg.airports if a.iata == "VIE")
    assert "hub" not in vie.tags


# --------------------------------------------------------------------------- #
# Unknown/misspelled tag detection                                             #
# --------------------------------------------------------------------------- #
def test_unknown_tags_detects_typo():
    reg = DestinationRegistry()
    assert reg.unknown_tags("seasid & italy") == ["seasid"]


def test_unknown_tags_case_insensitive_is_not_unknown():
    reg = DestinationRegistry()
    assert reg.unknown_tags("Italy") == []
    assert reg.unknown_tags("SEASIDE & Italy") == []


def test_unknown_tags_empty_when_all_known():
    reg = DestinationRegistry()
    assert reg.unknown_tags("seaside & (italy | spain)") == []


def test_known_tag_universe_includes_aliases_and_derived():
    reg = DestinationRegistry()
    universe = reg.known_tag_universe()
    assert "italian" in universe  # alias
    assert "hub" in universe      # derived
    assert "seaside" in universe  # terrain taxonomy


def test_get_reachable_unknown_bare_tag_returns_empty_and_logs(caplog):
    reg = DestinationRegistry()
    import logging
    with caplog.at_level(logging.WARNING, logger="flight_deals.registry.destinations"):
        result = reg.get_reachable("BUD", "Italyy")  # malformed/typo, no operators
    assert result == []
    assert any("unknown tags" in r.message for r in caplog.records)


def test_get_reachable_fallback_is_case_and_alias_aware():
    reg = DestinationRegistry()
    # "Italian" (capitalized alias) can't be parsed as a bare-token match by
    # matching()/where_parse only if something upstream chokes; the fallback
    # path must still resolve it the same way matching() would (lowercase +
    # alias expansion), not silently do a raw, case-sensitive get_by_tag.
    from flight_deals.registry.where import WhereParseError as _WPE

    class _BoomMatching(DestinationRegistry):
        def matching(self, expr, carrier_flags=None):
            raise _WPE("forced", hint="forced fallback for test")

    boomy = _BoomMatching.__new__(_BoomMatching)
    boomy.__dict__.update(reg.__dict__)
    got = {a.iata for a in boomy.get_reachable("BUD", "ITALIAN")}
    # Compare against the ordinary (non-forced) get_reachable success path for
    # the alias's target tag, so both sides go through the same
    # known-direct-route soft filter -- only the "how did we get `italy`
    # resolved" step differs (matching() vs. the WhereParseError fallback).
    expected = {a.iata for a in reg.get_reachable("BUD", "italy")}
    assert got == expected and got


def test_carrier_flags_graceful_offline(monkeypatch):
    reg = DestinationRegistry()

    class _Boom:
        def routes(self, origin, **kw):
            raise RuntimeError("offline")

    flags = reg.carrier_flags("BUD", ryanair=_Boom())
    # unknown, never fabricated
    assert all(v["ryanair"] is None and v["wizz"] is None for v in flags.values())
    # served tags simply don't match when unknown
    assert reg.matching("ryanair-served", flags) == []


def test_carrier_flags_populated_from_routes():
    reg = DestinationRegistry()

    class _FakeRy:
        def routes(self, origin, **kw):
            return ["CTA", "PMI"]

    flags = reg.carrier_flags("BUD", ryanair=_FakeRy())
    served = {a.iata for a in reg.matching("ryanair-served", flags)}
    assert served == {"CTA", "PMI"}
    assert flags["CTA"]["wizz"] is None  # Wizz has no route endpoint -> unknown


# --------------------------------------------------------------------------- #
# v2 dataset validation                                                        #
# --------------------------------------------------------------------------- #
def test_schema_version_and_shape():
    reg = DestinationRegistry()
    assert reg.schema_version == 2
    assert len(reg.airports) >= 70


def test_no_duplicate_iata():
    reg = DestinationRegistry()
    iatas = [a.iata for a in reg.airports]
    assert len(iatas) == len(set(iatas))


def test_every_airport_has_country_and_terrain_or_vibe():
    reg = DestinationRegistry()
    tv = TERRAIN_TAGS | VIBE_TAGS
    for a in reg.airports:
        tags = set(a.tags)
        assert tags & COUNTRY_TAGS, f"{a.iata} missing a country tag: {tags}"
        assert tags & tv, f"{a.iata} missing a terrain/vibe tag: {tags}"


def test_tags_are_in_known_taxonomy():
    reg = DestinationRegistry()
    known = COUNTRY_TAGS | REGION_TAGS | TERRAIN_TAGS | VIBE_TAGS | SEASONAL_TAGS
    for a in reg.airports:
        unknown = set(a.tags) - known
        assert not unknown, f"{a.iata} has off-taxonomy tags: {unknown}"


def test_coordinates_in_range():
    reg = DestinationRegistry()
    for a in reg.airports:
        assert -90 <= a.lat <= 90, a.iata
        assert -180 <= a.lon <= 180, a.iata


def test_referenced_iatas_exist():
    reg = DestinationRegistry()
    known = {a.iata for a in reg.airports}
    for city, iatas in reg.multi_city.items():
        for code in iatas:
            assert code in known, f"multi_city {city} references unknown {code}"
    for code in reg.origin_ground:
        assert code in known, f"origin_ground references unknown {code}"
    for pair in reg.open_jaw_pairs:
        assert pair["a"] in known and pair["b"] in known, f"open_jaw references unknown {pair}"


def test_origin_ground_and_open_jaw_seeds_present():
    reg = DestinationRegistry()
    assert set(reg.origin_ground) >= {"VIE", "BTS"}
    for e in reg.origin_ground.values():
        assert e["minutes"] > 0 and e["est_cost_eur"] > 0
    seed_pairs = {frozenset((p["a"], p["b"])) for p in reg.open_jaw_pairs}
    for a, b in [("NAP", "BRI"), ("BCN", "VLC"), ("ATH", "SKG"),
                 ("PMO", "CTA"), ("LIS", "OPO"), ("SPU", "ZAD")]:
        assert frozenset((a, b)) in seed_pairs, f"missing open-jaw seed {a}<->{b}"


def test_region_tags_imply_their_country_tag():
    """A region tag (sicily/sardinia/crete/cyclades/canaries/baleares) is
    shorthand for "this specific place within its country" — the country
    tag must always be present too, or country-level queries silently miss
    the airport (review item: region<->country coherence)."""
    region_to_country = {
        "canaries": "spain",
        "baleares": "spain",
        "sicily": "italy",
        "sardinia": "italy",
        "crete": "greece",
        "cyclades": "greece",
        "azores": "portugal",  # Task 18: PDL/TER carry both azores + portugal
    }
    reg = DestinationRegistry()
    violations = []
    for a in reg.airports:
        tags = set(a.tags)
        for region, country in region_to_country.items():
            if region in tags and country not in tags:
                violations.append((a.iata, region, country))
    assert not violations, f"region tags missing their country tag: {violations}"


def test_azores_airports_present_and_matchable():
    """Task 18: PDL (Ponta Delgada/São Miguel) + TER (Lajes/Terceira) are in the
    registry, tagged ``azores`` (a REGION_TAG) alongside ``portugal``, and reached
    by both ``where azores`` and ``where portugal``. ``azores`` is a known tag (not
    a typo) so the where-gate never burns a call refusing it."""
    reg = DestinationRegistry()
    by_iata = {a.iata: a for a in reg.airports}
    assert "PDL" in by_iata and "TER" in by_iata
    for iata in ("PDL", "TER"):
        tags = set(by_iata[iata].tags)
        assert "azores" in tags and "portugal" in tags
        assert "island" in tags  # a terrain tag so it passes the taxonomy check
    assert "azores" in REGION_TAGS
    assert reg.unknown_tags("azores") == []
    matched = {a.iata for a in reg.matching("azores")}
    assert matched == {"PDL", "TER"}
    assert {"PDL", "TER"} <= {a.iata for a in reg.matching("portugal")}


def test_lis_opo_are_s5_hubs():
    """Task 18: LIS + OPO join HUB_IATAS so a where=azores (or mainland
    Portugal/Spain) via-hub spec can compile self-transfer descriptors through
    them. Live routes(2026-07-13) confirm BUD<->LIS and BUD<->OPO are Ryanair-
    served — but NOT LIS/OPO->Azores (see test_lis_routes_fixture_has_no_azores)."""
    assert {"LIS", "OPO"} <= HUB_IATAS
    reg = DestinationRegistry()
    known = {a.iata for a in reg.airports}
    assert {"LIS", "OPO"} <= known


def test_brief_required_airports_present():
    reg = DestinationRegistry()
    known = {a.iata for a in reg.airports}
    required = {
        "CFU", "ZTH", "CHQ", "HER", "RHO", "JMK", "JTR", "KGS",
        "NAP", "BRI", "PMO", "CTA", "CAG", "AHO", "PSA", "BLQ", "VCE", "TRN",
        "MXP", "BGY", "FCO", "CIA", "SUF",
        "BCN", "ALC", "VLC", "AGP", "PMI", "IBZ", "MAH", "MAD",
        "TFS", "LPA", "ACE", "FUE", "LIS", "OPO", "FAO", "FNC",
        "SPU", "DBV", "ZAD", "VIE", "BTS", "STN", "LTN", "BVA", "BER",
        "PRG", "KRK", "WAW", "CRL",
    }
    missing = required - known
    assert not missing, f"missing brief airports: {missing}"


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def test_cli_where_list_json():
    result = runner.invoke(app, ["where", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["tags"]["seaside"] > 0
    assert data["aliases"]["italian"] == "italy"
    assert "hub" in data["derived"]


def test_cli_where_show_json():
    result = runner.invoke(app, ["where", "show", "seaside & (italy | spain)"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["count"] > 0
    iatas = {a["iata"] for a in data["airports"]}
    assert "CTA" in iatas


def test_cli_where_show_bad_expr_exit2_with_hint():
    result = runner.invoke(app, ["where", "show", "seaside &"])
    assert result.exit_code == 2
    payload = json.loads(result.output.strip().splitlines()[0])
    assert "error" in payload and payload["hint"]


def test_cli_where_show_pretty():
    result = runner.invoke(app, ["where", "show", "canaries", "--pretty"])
    assert result.exit_code == 0
    assert "TFS" in result.output


def test_cli_where_show_partial_unknown_tag_exit0_with_hint():
    # "italy" is real; "seasid" is a typo -> partial results, exit 0, hinted.
    result = runner.invoke(app, ["where", "show", "seasid & italy"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["unknown_tags"] == ["seasid"]
    assert "seaside" in data["hint"]  # difflib nearest match


def test_cli_where_show_all_unknown_tags_exit2_with_hint():
    result = runner.invoke(app, ["where", "show", "notarealtag"])
    assert result.exit_code == 2
    payload = json.loads(result.output.strip().splitlines()[0])
    assert payload["unknown_tags"] == ["notarealtag"]
    assert payload["hint"]


def test_cli_where_show_is_case_insensitive_no_unknown_tags():
    result = runner.invoke(app, ["where", "show", "Italy"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["count"] > 0
    assert "unknown_tags" not in data  # case difference is not "unknown"
