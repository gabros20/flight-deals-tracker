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

from flight_deals.models import Airport, Gem
from flight_deals.paths import resolve_path
from flight_deals.registry.where import WhereParseError, extract_identifiers, where_parse

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Gem season windows (Task 15) — "may-oct" style month ranges. Pure helpers so #
# both the registry (gems_matching) and the planner/engine can season-gate a   #
# gem against a search window without importing each other.                    #
# --------------------------------------------------------------------------- #
_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def season_months(season: str) -> Set[int]:
    """Month numbers a ``"may-oct"``/``"nov-feb"`` season covers (inclusive,
    wrap-around aware). Unparseable input -> empty set (never raises)."""
    try:
        lo_s, hi_s = season.lower().split("-")
        lo, hi = _MONTH_NUM[lo_s], _MONTH_NUM[hi_s]
    except (ValueError, KeyError, AttributeError):
        return set()
    if lo <= hi:
        return set(range(lo, hi + 1))
    return set(range(lo, 13)) | set(range(1, hi + 1))  # wraps the year end


def window_months(window: Optional[tuple]) -> Set[int]:
    """The calendar months an ``(out_from, out_to)`` ISO-date window touches."""
    if not window:
        return set()
    from datetime import date as _date
    start, end = _date.fromisoformat(window[0]), _date.fromisoformat(window[1])
    months, cur = set(), _date(start.year, start.month, 1)
    while cur <= end:
        months.add(cur.month)
        cur = _date(cur.year + 1, 1, 1) if cur.month == 12 else _date(cur.year, cur.month + 1, 1)
    return months


def season_overlaps_window(season: Optional[str], window: Optional[tuple]) -> bool:
    """True when a gem/gateway ``season`` overlaps the search ``window`` (or
    either is absent — no season means year-round; no window means "don't gate")."""
    if not season or not window:
        return True
    return bool(season_months(season) & window_months(window))


def gem_gateways_in_window(gem: Gem, window: Optional[tuple]) -> List["Any"]:
    """The gem's gateways whose EFFECTIVE season (gateway season, else the
    gem-level season, else year-round) overlaps ``window``. ``window=None`` ->
    all gateways (no season gating)."""
    out = []
    for gw in gem.gateways:
        effective = gw.season or gem.season
        if season_overlaps_window(effective, window):
            out.append(gw)
    return out


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
REGION_TAGS: Set[str] = {"sicily", "sardinia", "crete", "cyclades", "canaries", "baleares", "azores"}
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
    # Iberian hubs — the S5 gateway to the Azores (LIS->PDL/TER, OPO->PDL/TER
    # are Ryanair-served) and useful for mainland Portugal/Spain self-transfers.
    "LIS", "OPO",
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
    def __init__(self, data_path: Optional[str] = None,
                 ground_matrix_path: Optional[str] = None):
        self.data_path = resolve_path(data_path or "data/destinations.json")
        self._ground_matrix_path = ground_matrix_path  # None -> default committed matrix
        self.airports: List[Airport] = []
        self.schema_version: int = 2
        self.multi_city: Dict[str, List[str]] = {}
        self.origin_ground: Dict[str, Dict[str, Any]] = {}
        self.open_jaw_pairs: List[Dict[str, Any]] = []
        self.gems: List[Gem] = []
        self._open_jaw_merged: Optional[List[Dict[str, Any]]] = None
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
            self._load_gems(data.get("gems", []))
        else:  # legacy v1 (bare list) — tolerate for safety
            self.schema_version = 1
            self.airports = [Airport(**item) for item in data]

    def _load_gems(self, raw_gems: List[Dict[str, Any]]) -> None:
        """Validate + load the ``gems`` array (Task 15). A gem that fails
        validation, references an unknown gateway airport, or carries an
        off-taxonomy tag is skipped with a warning (Global Constraint 3: never
        silently include broken data), so one bad entry can't poison the sweep."""
        from pydantic import ValidationError

        known_iatas = self._iatas()
        tag_universe = TERRAIN_TAGS | VIBE_TAGS | REGION_TAGS | COUNTRY_TAGS | SEASONAL_TAGS
        for item in raw_gems:
            try:
                gem = Gem(**item)
            except ValidationError as e:
                logger.warning("registry: skipping invalid gem %r: %s", item.get("slug"), e)
                continue
            bad_gw = [gw.airport for gw in gem.gateways if gw.airport not in known_iatas]
            if bad_gw:
                logger.warning("registry: skipping gem %r — unknown gateway airport(s) %s",
                               gem.slug, bad_gw)
                continue
            bad_tags = [t for t in gem.tags if t not in tag_universe]
            if bad_tags:
                logger.warning("registry: gem %r carries off-taxonomy tag(s) %s (kept, but "
                               "they can never match a where-expression)", gem.slug, bad_tags)
            self.gems.append(gem)

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

    # ------------------------------------------------------------------ #
    # Gem matching (Task 15) — a SEPARATE seam from airport matching()    #
    # ------------------------------------------------------------------ #
    # DESIGN CHOICE (per Task 15 brief): ``matching()`` stays airport-only and
    # network-free; gems are matched by this distinct ``gems_matching()`` which
    # additionally season-gates against the search window. Keeping them apart
    # means the deterministic ``compile``/``plan`` path (which fans out Wizz TT
    # per airport) is untouched by gems — a gem contributes its gateway airports
    # to the fare FILTER at execute-time (planner.execute) and an onward
    # extension directive at intent-time, never new planned calls. See the Task
    # 15b report + SEARCH-DESIGN §2b for the full rationale.
    def _gem_tagset(self, gem: Gem) -> Set[str]:
        """A gem's matchable tag set: its curated tags PLUS its country as a
        tag (gems carry ``country: "Greece"`` but not a ``greece`` tag), so
        ``island & greece`` can reach a Greek gem."""
        return set(gem.tags) | {gem.country.lower()}

    def gems_matching(self, expr: str, *, window: Optional[tuple] = None,
                      include_marginal: bool = False) -> List[Gem]:
        """Gems whose tags satisfy the where-expression ``expr``. KEEP gems only
        by default (``marginal`` gems are reachable via ``--to``, not category
        matching). When ``window`` is given, a gem is included only if it has at
        least one gateway whose effective season overlaps that window (a wholly
        out-of-season gem is dropped). Raises :class:`WhereParseError` on a bad
        expression (same as :meth:`matching`)."""
        node = where_parse(expr, ALIASES)
        out: List[Gem] = []
        for gem in self.gems:
            if gem.marginal and not include_marginal:
                continue
            if not node.eval(self._gem_tagset(gem)):
                continue
            if window is not None and not gem_gateways_in_window(gem, window):
                continue  # every gateway out of season for this window
            out.append(gem)
        return out

    def resolve_gem(self, value: str) -> Optional[Gem]:
        """Resolve a ``--to`` value to a gem by exact slug or case-insensitive
        name (``"halki"`` / ``"Halki"``). ``None`` on a miss — the caller then
        falls back to airport/city resolution, and only if THAT also misses does
        it emit a did-you-mean hint (which now includes gem names/slugs)."""
        if not value or not str(value).strip():
            return None
        low = str(value).strip().lower()
        for gem in self.gems:
            if gem.slug == low or gem.name.lower() == low:
                return gem
        return None

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

    def tag_hint(self, unknown: List[str]) -> str:
        """A "did you mean" hint for each unknown tag — nearest known tag in
        :meth:`known_tag_universe` by ``difflib`` (cutoff 0.6), or "is unknown"
        when nothing is close. Shared by ``where show`` and the where-gate
        that protects ``getaway``/``oneway``/``run``/``watch add`` from
        burning a network call over a typo'd ``--where`` (SEARCH-DESIGN §3)."""
        import difflib
        known_universe = sorted(self.known_tag_universe())
        suggestions = []
        for tag in unknown:
            close = difflib.get_close_matches(tag, known_universe, n=1, cutoff=0.6)
            suggestions.append(f"{tag!r} - did you mean: {close[0]}?" if close else f"{tag!r} is unknown")
        return "; ".join(suggestions)

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
    # Named-destination resolution (--to: IATA code OR city name)         #
    # ------------------------------------------------------------------ #
    def resolve_destination(self, value: str) -> Optional[List[str]]:
        """Resolve a ``--to`` value to a list of destination IATAs present in
        the registry, or ``None`` on a miss.

        Order (task 9 C1): case-insensitive **city name** first (a multi-airport
        city — ``multi_city`` group — expands to all its member airports present
        in the registry; e.g. ``milan`` -> ``["BGY", "MXP"]``), then a 3-letter
        **IATA** code. Never raises — the caller turns ``None`` into an exit-2
        did-you-mean hint via :meth:`destination_suggestion`.
        """
        if not value or not str(value).strip():
            return None
        raw = str(value).strip()
        lower = raw.lower()
        known = self._iatas()

        # 1a. Multi-airport city group (canonical city name is the group key).
        for city, iatas in self.multi_city.items():
            if city.lower() == lower:
                members = sorted(i for i in iatas if i in known)
                if members:
                    return members
        # 1b. Single-airport city: exact match, else the city's leading token
        # (airport cities read "Milan Malpensa"/"London Stansted").
        city_hits = sorted({a.iata for a in self.airports if a.city.lower() == lower})
        if not city_hits:
            city_hits = sorted({a.iata for a in self.airports
                                if a.city.lower().split()[0:1] == [lower]})
        if city_hits:
            return city_hits

        # 2. IATA code.
        up = raw.upper()
        if len(up) == 3 and up in known:
            return [up]
        return None

    def destination_suggestion(self, value: str) -> Optional[str]:
        """Nearest known city name, gem name, or IATA for a ``--to`` miss
        (did-you-mean). Gem names AND slugs are in the pool so a typo like
        ``"halky"`` suggests ``"Halki"``."""
        import difflib
        raw = str(value).strip()
        gem_names = {g.name for g in self.gems} | {g.slug for g in self.gems}
        cities = sorted({a.city for a in self.airports} | set(self.multi_city.keys()) | gem_names)
        close = difflib.get_close_matches(raw, cities, n=1, cutoff=0.6)
        if not close:
            close = difflib.get_close_matches(raw.lower(), sorted(g.slug for g in self.gems), n=1, cutoff=0.6)
        if not close:
            close = difflib.get_close_matches(raw.upper(), sorted(self._iatas()), n=1, cutoff=0.6)
        return close[0] if close else None

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
        """Curated open-jaw pairs (authoritative, unchanged values,
        ``estimate_basis="curated"``) merged with the computed ground matrix
        (``data/ground_matrix.json`` via :mod:`registry.ground_matrix`,
        ``estimate_basis="computed"``). A curated ``{a, b}`` combo is never
        duplicated or overridden by a computed one. Tolerant when the matrix
        file is absent — curated-only (Task 11 req 3). Cached after first load."""
        if self._open_jaw_merged is None:
            from flight_deals.registry import ground_matrix
            computed = ground_matrix.load_ground_matrix(self._ground_matrix_path)
            ground_matrix.check_airport_drift(self._iatas(), self._ground_matrix_path)
            self._open_jaw_merged = ground_matrix.merge_open_jaw_pairs(
                self.open_jaw_pairs, computed,
            )
        return list(self._open_jaw_merged)
