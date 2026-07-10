"""Airport registry v2 + the ``--where`` tag algebra.

Loads ``data/destinations.json`` (schema_version 2): audited airports with
country/region/terrain/vibe tags, ``multi_city`` groups, ``origin_ground``
(BUD extended-origin ground legs) and ``open_jaw_pairs`` seeds. Exposes tag
matching over the where-expression language and best-effort, lazily-fetched
per-carrier service flags (graceful ``unknown`` when offline).
"""
import json
import logging
from typing import Any, Dict, List, Optional, Set

from flight_deals.models import Airport
from flight_deals.paths import resolve_path
from flight_deals.registry.where import WhereParseError, extract_identifiers, where_parse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Taxonomy vocabularies (SEARCH-DESIGN §3) — used for dataset validation.      #
# --------------------------------------------------------------------------- #
COUNTRY_TAGS: Set[str] = {
    "italy", "spain", "greece", "portugal", "croatia", "france", "germany",
    "poland", "austria", "slovakia", "czech", "hungary", "uk", "ireland",
    "netherlands", "belgium", "bulgaria", "albania", "malta", "turkey",
    "morocco", "scandinavia", "balkans", "benelux",
}
# Island/region groups where the group is the real unit. Extra to a country tag.
REGION_TAGS: Set[str] = {"sicily", "sardinia", "crete", "cyclades", "canaries", "baleares"}
TERRAIN_TAGS: Set[str] = {"seaside", "island", "mountains", "lakes", "thermal"}
VIBE_TAGS: Set[str] = {
    "city-break", "party", "quiet", "hidden-gem", "shopping", "family",
    "culture", "hiking", "winter-sun", "ski",
}
SEASONAL_TAGS: Set[str] = {"seasonal-summer", "seasonal-winter"}
# Auto-derived at query time, never hand-curated in the data file.
DERIVED_TAGS: Set[str] = {"ryanair-served", "wizz-served", "hub"}

# Static hub curation (pending route-network-derived connectivity, §8). These
# get a synthetic ``hub`` tag injected at match time.
HUB_IATAS: Set[str] = {
    "VIE", "BTS", "BGY", "MXP", "FCO", "CIA", "BCN", "MAD", "STN", "LTN",
    "LGW", "BER", "AMS", "DUB", "IST", "SAW", "WAW", "PRG",
}

# Aliases expand to sub-expressions before evaluation. Synonyms + a few
# backward-compat names for the old v1 coarse tags.
ALIASES: Dict[str, str] = {
    # nationality synonyms -> country tag
    "italian": "italy",
    "spanish": "spain",
    "greek": "greece",
    "portuguese": "portugal",
    "croatian": "croatia",
    "french": "france",
    "german": "germany",
    "polish": "poland",
    # island-group synonyms
    "greek-islands": "island & greece",
    "canary-islands": "canaries",
    "balearic": "baleares",
    "balearic-islands": "baleares",
    # backward-compat with v1 coarse tags (old CLI/tests)
    "european-islands": "island",
    "italian-gems": "italy",
}


# Known direct connections from popular Ryanair & Wizz bases (soft filter used
# by get_reachable when route data is not available). Extendable.
KNOWN_DIRECT_ROUTES: Dict[str, Set[str]] = {
    "BUD": {
        "PMI", "IBZ", "MAH", "CFU", "HER", "CHQ", "ZTH", "JTR", "RHO", "PVK",
        "KLX", "EFL", "JMK", "KGS", "SKG", "ATH", "CTA", "PMO", "CAG", "OLB",
        "AHO", "ALC", "AGP", "FAO", "VLC", "GRO", "CDT", "LPA", "TFS", "ACE",
        "FUE", "FNC", "BRI", "SUF", "BDS", "NAP", "VCE", "PSA", "BLQ", "DBV",
        "ZAD", "SPU", "MLA", "LIS", "OPO", "BGY", "BCN", "MAD", "STN", "EDI",
        "PRG", "KRK", "DUB", "VIE", "BER", "BOJ", "VAR", "TIA",
    },
}


class DestinationRegistry:
    def __init__(self, data_path: Optional[str] = None):
        self.data_path = resolve_path(data_path or "data/destinations.json")
        self.airports: List[Airport] = []
        self.schema_version: int = 2
        self.multi_city: Dict[str, List[str]] = {}
        self.origin_ground: Dict[str, Dict[str, Any]] = {}
        self.open_jaw_pairs: List[Dict[str, Any]] = []
        self._carrier_cache: Dict[str, Dict[str, Optional[bool]]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Loading                                                            #
    # ------------------------------------------------------------------ #
    def _load(self):
        if not self.data_path.exists():
            logger.warning("registry: destinations file not found at %s; registry empty", self.data_path)
            return
        data = json.loads(self.data_path.read_text())
        if isinstance(data, dict) and "airports" in data:  # v2 schema
            self.schema_version = int(data.get("schema_version", 2))
            self.airports = [Airport(**item) for item in data["airports"]]
            self.multi_city = data.get("multi_city", {})
            self.origin_ground = data.get("origin_ground", {})
            self.open_jaw_pairs = data.get("open_jaw_pairs", [])
        else:  # legacy v1 (bare list) — tolerate for safety
            self.schema_version = 1
            self.airports = [Airport(**item) for item in data]

    def _iatas(self) -> Set[str]:
        return {a.iata for a in self.airports}

    # ------------------------------------------------------------------ #
    # Where-algebra matching                                             #
    # ------------------------------------------------------------------ #
    def _tagset(self, airport: Airport,
                carrier_flags: Optional[Dict[str, Dict[str, Optional[bool]]]] = None) -> Set[str]:
        tags = set(airport.tags)
        if airport.iata in HUB_IATAS:
            tags.add("hub")
        if carrier_flags:
            cf = carrier_flags.get(airport.iata, {})
            if cf.get("ryanair"):
                tags.add("ryanair-served")
            if cf.get("wizz"):
                tags.add("wizz-served")
        return tags

    def matching(self, expr: str,
                 carrier_flags: Optional[Dict[str, Dict[str, Optional[bool]]]] = None) -> List[Airport]:
        """Return airports whose tags satisfy the where-expression ``expr``.

        Pure over static tags by default (no network). Pass ``carrier_flags``
        (from :meth:`carrier_flags`) to make ``ryanair-served``/``wizz-served``
        resolvable. Raises :class:`WhereParseError` on malformed input.
        """
        node = where_parse(expr, ALIASES)
        return [a for a in self.airports if node.eval(self._tagset(a, carrier_flags))]

    def known_tag_universe(self) -> Set[str]:
        """All identifiers a where-expression may legally reference: every
        taxonomy tag set, derived (auto) tags, and alias names. Used to
        detect unknown/misspelled tags instead of silently matching nothing.
        """
        return (
            COUNTRY_TAGS | REGION_TAGS | TERRAIN_TAGS | VIBE_TAGS | SEASONAL_TAGS
            | DERIVED_TAGS | set(ALIASES.keys())
        )

    def unknown_tags(self, expr: str) -> List[str]:
        """Identifiers referenced in ``expr`` (lower-cased) that are not part
        of :meth:`known_tag_universe` — i.e. likely typos/misspellings such
        as ``"seasid"`` or off-taxonomy tags such as ``"italy-ish"``.

        Case is not a source of "unknown": identifiers are lower-cased at
        tokenize time (see ``registry.where``), so ``Italy`` == ``italy`` and
        is never reported here.
        """
        known = self.known_tag_universe()
        idents = extract_identifiers(expr)
        return sorted(t for t in idents if t not in known)

    def where_list(self) -> Dict[str, Any]:
        """Tag inventory with counts, aliases, and derived (auto) tags."""
        counts: Dict[str, int] = {}
        for a in self.airports:
            for t in a.tags:
                counts[t] = counts.get(t, 0) + 1
        return {
            "tags": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "aliases": dict(sorted(ALIASES.items())),
            "derived": sorted(DERIVED_TAGS),
        }

    # ------------------------------------------------------------------ #
    # Carrier service flags (lazy, best-effort, graceful when offline)   #
    # ------------------------------------------------------------------ #
    def carrier_flags(self, origin: str = "BUD", ryanair=None,
                      use_cache: bool = True) -> Dict[str, Dict[str, Optional[bool]]]:
        """Best-effort per-carrier served flags for every airport, from the
        given origin. ``True``/``False`` for Ryanair when the route network is
        reachable, else ``None`` (unknown). Wizz has no public route endpoint,
        so it is always ``None`` (unknown) — surfaced honestly, never faked.
        """
        origin = (origin or "BUD").upper()
        if use_cache and origin in self._carrier_cache:
            return self._carrier_cache[origin]

        ryanair_served: Optional[Set[str]] = None
        if ryanair is None:
            try:
                from flight_deals.providers.ryanair import RyanairProvider
                ryanair = RyanairProvider()
            except Exception as e:  # provider import/construction failure
                logger.warning("registry: ryanair provider unavailable: %s", e)
        if ryanair is not None:
            try:
                ryanair_served = set(ryanair.routes(origin))
            except Exception as e:  # offline / blocked / schema drift
                logger.warning("registry: ryanair routes(%s) unavailable: %s", origin, e)
                ryanair_served = None

        flags: Dict[str, Dict[str, Optional[bool]]] = {}
        for a in self.airports:
            flags[a.iata] = {
                "ryanair": (a.iata in ryanair_served) if ryanair_served is not None else None,
                "wizz": None,
            }
        self._carrier_cache[origin] = flags
        return flags

    # ------------------------------------------------------------------ #
    # Reachability (used by the orchestrator's one-way search)           #
    # ------------------------------------------------------------------ #
    def get_by_tag(self, tag: str) -> List[Airport]:
        return [a for a in self.airports if tag in a.tags]

    def get_by_origin(self, origin_iata: str) -> List[Airport]:
        return [a for a in self.airports if a.iata != origin_iata]

    def get_reachable(self, origin: str, category: Optional[str] = None) -> List[Airport]:
        """Destinations likely reachable direct from ``origin``.

        ``category`` may be any where-expression (``seaside & (italy|spain)``)
        or a bare tag/alias (old v1 names like ``european-islands`` still work
        via aliases). Falls back to all matches if the origin's known-route set
        is unavailable.
        """
        if category:
            try:
                candidates = self.matching(category)
            except WhereParseError:
                # Tolerate a malformed/bare token as a plain tag (never crash
                # a sweep) — but apply the *same* lowercase + alias handling
                # that matching()/where_parse would have used, so this
                # fallback doesn't silently behave differently (review item:
                # get_reachable dual-path inconsistency).
                key = category.strip().lower()
                resolved = ALIASES.get(key, key)
                if any(op in resolved for op in "&|!()"):
                    try:
                        candidates = self.matching(resolved)
                    except WhereParseError:
                        candidates = self.get_by_tag(key)
                else:
                    candidates = self.get_by_tag(resolved)
            if not candidates:
                unknown = self.unknown_tags(category)
                if unknown:
                    logger.warning(
                        "registry: get_reachable(origin=%s, category=%r) matched no "
                        "airports; unknown tags: %s", origin, category, unknown,
                    )
        else:
            candidates = list(self.airports)
        candidates = [a for a in candidates if a.iata != origin]

        known = KNOWN_DIRECT_ROUTES.get(origin.upper(), set())
        if known:
            reachable = [a for a in candidates if a.iata in known]
            if reachable:
                return reachable
        return candidates

    def get_all_tags(self) -> Set[str]:
        tags: Set[str] = set()
        for a in self.airports:
            tags.update(a.tags)
        return tags

    # ------------------------------------------------------------------ #
    # Multi-city / ground / open-jaw accessors                           #
    # ------------------------------------------------------------------ #
    def get_multi_airport_cities(self) -> List[str]:
        return list(self.multi_city.keys())

    def get_airports_for_multi_city(self, city: str) -> List[str]:
        return list(self.multi_city.get(city, []))

    def get_all_multi_airport_airports(self) -> List[str]:
        out: List[str] = []
        for iatas in self.multi_city.values():
            out.extend(iatas)
        return out

    def get_ground_transfer_pairs(self) -> List[tuple]:
        pairs = []
        for iatas in self.multi_city.values():
            for i in range(len(iatas)):
                for j in range(i + 1, len(iatas)):
                    pairs.append((iatas[i], iatas[j]))
                    pairs.append((iatas[j], iatas[i]))
        return pairs

    def get_origin_ground(self, iata: str) -> Optional[Dict[str, Any]]:
        return self.origin_ground.get(iata.upper())

    def get_open_jaw_pairs(self) -> List[Dict[str, Any]]:
        return list(self.open_jaw_pairs)
