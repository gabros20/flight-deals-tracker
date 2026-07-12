# Output Contract (frozen, Phase 0.5)

This document is the frozen schema for every `flight-deals` command's stdout.
Tasks 3-8 implement against it; they do not invent fields. Anything left open
by `docs/UPGRADE-PLAN.md` / `docs/SEARCH-DESIGN.md` is decided **here**, with
the decision recorded inline so it isn't re-litigated per-task.

This doc describes the target shape. It is a contract for Task 6 (envelope
implementation) and beyond — it is NOT implemented by Task 2. `tests/
test_contract.py` validates it against hand-built examples and against the
raw provider fixtures captured alongside it, not against CLI output (there is
none yet).

---

## 1. The envelope

Every command prints exactly one JSON object to stdout (default) or, with
`--pretty`, a human-rendered version of the *same* fields via the single
`output.py` renderer (no second data path). Top level:

```jsonc
{
  "results": [ /* list of Deal objects, § 2 below; [] is a valid, successful
                  answer — see route_status */ ],
  "summary": "3 deals found, cheapest €89 BUD→CFU (27% below typical)",
  "sources": {
    "ryanair": "ok",
    "wizzair": "version_refreshed"
  },
  "next": [
    "flight-deals check a48e258b18",
    "flight-deals watch add BUD-CFU --months 2026-08 --nights 5-7"
  ],

  // present only when results == [] (see § 4)
  "route_status": "no_service",

  // present only on exit-code 1 or 2 (§ 3)
  "error": "invalid_iata",
  "hint": "did you mean 'BUD'? run: flight-deals search -c seaside --date-from 2026-08-22 --date-to 2026-08-24"
}
```

Field rules:
- `results`: always present, always a list (never `null`). Order is rank
  order (best first) once ranking exists (Task 7); until then, provider
  order is acceptable.
- `summary`: always present, always a single sentence, always safe to paste
  verbatim into a Telegram message (no JSON, no markdown tables). On empty
  results it explains *why* in plain language, sourced from `route_status`.
  When `results` is non-empty but a provider needed to answer the query
  failed/blocked/parse-errored (see § 3 "Partial coverage"), `summary` MUST
  append the coverage gap in plain language, e.g. `"...(Wizz Air
  unavailable — results may be incomplete)"` — never silently reported as a
  clean success.
- `sources`: always present, one key per provider **queried during this
  call** (a provider not relevant to the request — e.g. Wizz for a
  Wizz-unserved route — is simply absent, not `"not_queried"`, since it was
  never invoked). Status values (open enum, extend as needed but do not
  repurpose these):
  - `ok` — succeeded, data used.
  - `error` — request/parse failed; nothing usable from this provider this
    run (typed exception at the provider layer, per Task 3's `http.py`).
  - `blocked` — 403/429 after retries exhausted (rate-limited or
    fingerprinted); distinct from generic `error` because the remedy is
    "wait", not "investigate".
  - `version_refreshed` — Wizz-specific: the cached API version 404'd, the
    provider re-scraped and retried once, and the retry succeeded.
  - `parse_error` — 200 response but the body didn't match the expected
    schema (`SchemaError` from Task 3's `http.py`); never silently treated
    as "no results".
- `next`: always present (may be `[]`). Each entry is a **complete,
  copy-pasteable** `flight-deals ...` command string, not a description.
  Agents (Hermes) must be able to run `next[0]` verbatim.
- `error` / `hint`: present together, only when exit code is 1 or 2, and
  always both present when either is (never one without the other). `hint`
  is not generic advice — it is either an exact corrected command (exit 2)
  or a plain statement of what will happen automatically (exit 1, e.g.
  `error: "provider_error"`, `hint: "the next scheduled run will retry; or
  re-run with --fresh"`).

**What's frozen vs. what isn't:** `deal_id`, field names, field types, and
enum values (`shape`, `price_confidence`, `sources` status values,
`route_status`) are frozen by this contract — two conformant
implementations must agree on them byte-for-byte. `summary`, `why` (§2),
and `next`'s prose/wording, and the *ordering* of keys within `sources` or
of entries within `results` (beyond the ranking rule above), are **not**
byte-stable across implementations — they may be templated, generated, or
reordered differently by different code. Don't diff/assert on those for
byte-identical envelopes; assert on the frozen fields instead.

---

## 2. The Deal object

```jsonc
{
  "deal_id": "a48e258b18",
  "shape": "S2",
  "origin": "BUD",
  "destination": "CFU",
  "out_date": "2026-08-22",
  "return_date": "2026-08-27",
  "nights": 5,
  "price_eur": 89.98,
  "price_confidence": "exact",
  "carriers": ["ryanair"],
  "legs": [
    {
      "type": "flight",
      "origin": "BUD",
      "destination": "CFU",
      "carrier": "ryanair",
      "departure_date": "2026-08-22",
      "departure_time": "10:35",
      "flight_number": "FR 1234",
      "price_eur": 44.99,
      "duration_minutes": 105
    },
    {
      "type": "flight",
      "origin": "CFU",
      "destination": "BUD",
      "carrier": "ryanair",
      "departure_date": "2026-08-27",
      "departure_time": "22:10",
      "flight_number": "FR 1235",
      "price_eur": 44.99,
      "duration_minutes": 100
    }
  ],
  "ground": null,
  "why": "€89 vs typical €140 for this route, 36% below, 42 observations",
  "links": {
    "ryanair": "https://www.ryanair.com/gb/en/trip/flights/select?originIata=BUD&destinationIata=CFU&dateOut=2026-08-22&dateIn=2026-08-27&adults=1"
  }
}
```

Field-by-field:

- **`deal_id`**: see § 5. Stable across re-runs of the same query
  regardless of price movement (price is deliberately excluded from the
  hash — the same trip re-priced tomorrow is the same deal for `check`/
  snapshot purposes).
- **`shape`**: one of the trip-shape codes from `SEARCH-DESIGN.md` §2,
  reused verbatim rather than inventing new names:
  - `S1` direct one-way
  - `S2` direct round-trip
  - `S3` extended-origin (ground leg to a nearby hub, then S1/S2)
  - `S4` open-jaw (fly into D1, ground to D2, fly home from D2)
  - `S5` self-transfer via hub (two tickets, same-airport connection)
  (S6 multi-stop nomad is out of scope per SEARCH-DESIGN §2 and never
  appears here.)
- **`origin` / `destination`**: uppercase 3-letter IATA. For S3/S4/S5 these
  are the *trip's* endpoints (the airport the traveller starts/ends at),
  not an intermediate leg — intermediate airports appear only inside
  `legs`/`ground`.
- **`out_date` / `return_date`**: ISO `YYYY-MM-DD`, airport-local calendar
  date (Global Constraint 6). `return_date` is `null` for one-way (`S1`)
  deals.
- **`nights`**: integer, `(return_date - out_date).days`; `null` for
  one-way deals. Computed, never independently supplied, so it can never
  disagree with the dates.
- **`price_eur`**: total trip price in EUR (Global Constraint 4). Always
  the *converted* value; `currency_original`/raw amounts live only on
  individual `legs` entries if a provider returns non-EUR (Wizz), never at
  the Deal level.
- **`price_confidence`**: `exact` (Ryanair farfnd) or `approximate` (Wizz
  timetable, ±10% per UPGRADE-PLAN §3). A Deal combining legs of different
  confidence (e.g. S5) reports the *weakest* confidence present —
  `approximate` if any leg is approximate.
- **`carriers`**: list, not a single string, even for a one-carrier deal
  (`["ryanair"]`) — S3/S4/S5 shapes can mix `ryanair` and `wizzair`.
  Lowercase carrier ids: `ryanair`, `wizzair`. Sorted alphabetically so the
  list order is deterministic (feeds `deal_id`, § 5).
- **`legs`**: ordered list, chronological. Each entry is either a flight leg
  or a ground leg:
  - Flight leg: `type: "flight"`, `origin`, `destination`, `carrier`,
    `departure_date`, `departure_time` (nullable — day-level data like
    CAL/TT has no time; `null` until an RT-EXACT/AVAIL confirmation adds
    it), `flight_number` (nullable, same reason), `price_eur`,
    `duration_minutes` (nullable).
  - Ground leg: `type: "ground"`, `from_iata`, `to_iata`, `mode`
    (`"driving"|"public_transit"|"train"|"bus"|...`), `duration_minutes`,
    `distance_km` (nullable — a static curated hop has no routed distance),
    `cost_eur` (nullable — estimate). Matches `models.GroundLeg`, whose cost
    field was renamed `estimated_cost_eur` -> `cost_eur` in Task 10 (§ 7 open
    item RESOLVED); the model still accepts the legacy key on input via a
    validation alias.
- **`ground`**: `null` for `S1`/`S2`. For `S3`/`S4`/`S5`, a **summary**
  object so a consumer doesn't have to walk `legs` to answer "how much
  ground transfer is involved":
  ```jsonc
  { "duration_minutes": 150, "cost_eur": 12.0, "mode": "public_transit",
    "estimate_basis": "computed" }
  ```
  This is a convenience mirror of the ground leg(s) already present in
  `legs`, not new data. `estimate_basis` (additive, Task 11) is `"curated"`
  for a hand-verified hop (the 6 curated open-jaw pairs, the VIE/BTS
  extended-origin legs), `"computed"` for one derived from the OSRM ground
  matrix (`data/ground_matrix.json`), `"scheduled"` (additive, Task 13)
  for a computed pair whose modeled duration was refined by a real
  Transitous/MOTIS timetable itinerary, or `"scheduled-hybrid"` (additive,
  Task 14) for a pair whose CITY line-haul is scheduled but whose
  airport-access pads are modeled; absent when no provenance is attached.
  A `"scheduled"` / `"scheduled-hybrid"` hop also carries additive
  `transit_transfers` (the number of transfers in that itinerary). A pure
  `"scheduled"` hop's `duration_minutes` is the real scheduled length and its
  `why` clause drops the `~` on the duration (keeping `~` on the modeled
  `cost_eur`). A `"scheduled-hybrid"` hop KEEPS the `~` on duration (the
  access pads are modeled) and its `why` clause says "line-haul scheduled".
  `has_ferry` (additive, Task 12) is `true` when the ground hop crosses water on
  a ferry (a curated ferry corridor or a computed `ferry+ground` matrix pair);
  the `why` string then leads the hop with ⛴ so an agent discloses the crossing.
  Absent (never `false`) when no ferry is involved, so non-ferry deals stay
  byte-identical.
- **`why`**: one sentence, always includes a number and a comparison basis
  (`"vs typical €X"`, `"N observations"`) per SEARCH-DESIGN §2. Never a bare
  adjective ("great deal!") — must be falsifiable/re-derivable from
  `history`. Before price history exists (state/store.py, Task 8), `why`
  degrades to a factual, non-comparative sentence (e.g. `"only Ryanair
  fare found for this route/window"`) rather than fabricating a percentile.
- **`links`**: map of carrier id -> booking URL. Only carriers actually
  present in `carriers` get an entry. Absent (not `null`) if a booking deep
  link can't be constructed confidently for a shape (e.g. S5 self-transfer
  is two separate bookings — both keyed by leg, not one combined link).

### 2a. Shaped deals — S3 extended-origin, S4 open-jaw (Task 10)

Both are additive: they reuse the exact Deal shape above; only `shape`,
`legs`, `ground`, and the endpoint semantics differ. Enabled on `getaway` via
`--shapes` (default `direct` — NOT default-enabled) and on a spec via
`shapes:[…]`.

- **S3 extended-origin** (`shape: "S3"`): the traveller starts at the base
  origin (BUD), takes ground to a nearby airport with a big low-cost base
  (VIE/BTS), flies a **round-trip** from there, and grounds back.
  - `origin` = base origin (BUD), `destination` = the flown destination D. The
    extended airport (VIE) appears only inside `legs`.
  - `legs` (chronological): `ground BUD→VIE`, `flight VIE→D`, `flight D→VIE`,
    `ground VIE→BUD` — i.e. **two** ground legs (out + back).
  - `ground` summary carries the **total** ground `duration_minutes` and
    `cost_eur` across both legs (2×).
  - `price_eur` = round-trip fare + 2×ground cost. `price_confidence: exact`
    (Ryanair RT-ANYWHERE). `carriers: ["ryanair"]`.
- **S4 open-jaw** (`shape: "S4"`): fly into D1, ground D1→D2, fly home from D2 —
  two separate one-way tickets that form one bookable trip.
  - `origin` = base origin (BUD). `destination` = **D1, the fly-in airport**
    (the outbound leg's arrival). The fly-home airport D2 and the hop appear
    only inside `legs`; the two cities are a single two-city product, so this is
    the one shape whose `destination` alone doesn't name the whole trip — read
    `legs`/`ground` for the D2 leg.
  - `legs` (chronological): `flight BUD→D1`, `ground D1→D2`, `flight D2→BUD` —
    **one** ground hop.
  - `ground` summary carries the single hop's `duration_minutes`/`cost_eur`.
  - `price_eur` = leg1 + leg2 + ground cost. `price_confidence: exact` (Ryanair
    CAL/OW per leg) — but note it is **two separate tickets**, no single
    combined booking `link`. `carriers: ["ryanair"]`.

`deal_id` (§ 5) is unchanged: it already includes `shape`, so an S3/S4 deal to
D never collides with the S2 direct deal to D. `why` carries the honest ground
clause ("incl. ~€42 bus BUD⇄VIE, 2×2h45m" / "fly into NAP, train ~4h €35, fly
home from BRI").

Ranking is across shapes by total `price_eur` (fares + ground). S1/S2/S3 to the
same destination are deduplicated (cheapest wins, so an extended origin only
surfaces when it genuinely beats direct); S4 is a distinct product keyed by the
unordered airport pair and is never deduped against a direct deal.

---

## 3. Exit codes

| Code | Meaning | `results` | `error`/`hint` |
|---|---|---|---|
| 0 | OK — command completed and the envelope is trustworthy, **including when `results` is `[]`** (a typed empty state, § 4, is a successful answer) **and including when a provider failed but at least one other provider still produced usable results** (see "Partial coverage" below) | `[]` or populated | absent |
| 1 | Transient/provider failure — `results` is empty **and** at least one provider needed to answer this query failed/blocked/parse-errored, so the emptiness itself is untrustworthy; the caller should not conclude "no deals exist", only "this run couldn't tell". Retry = the next scheduled run (cron) or a manual re-run | `[]` only | present |
| 2 | Input error — bad IATA, unparsable dates, invalid `--where` expression, invalid flag combination; caught **before** any network call | `[]` | present, `hint` is an exact corrected command |

Rule of thumb: exit 1 is "the world didn't cooperate **and** left us with
nothing", exit 2 is "the request was malformed" (agent's mistake, fixable
without retrying anything). A provider being down never becomes exit 2, and
a bad flag never becomes exit 1.

**Partial coverage is exit 0, not exit 1.** If a provider fails but at
least one other provider still produced ≥1 usable result, the run is exit
0: the caller has something usable *right now*, and retrying wouldn't
change that. The failure is not hidden — it's visible in two places:
- `sources` carries the failing provider's real status (`error` /
  `blocked` / `parse_error`), never silently `ok`;
- `summary` names the gap in plain language, e.g. `"...found 2 deals...
  (Wizz Air unavailable — results may be incomplete)"`, so an agent/human
  reading only the sentence still learns coverage was partial.

Exit 1 stays reserved for the case where that safety net doesn't exist —
`results == []` with a provider failure — because that's the only case
where "no deals" and "we couldn't check" are indistinguishable without the
`error`/`hint` signal.

Stub commands (Task 1: `roundtrip`, `collect`, `alerts`, `history`,
`multi_airports`, `--connections`) exit 2 with
`{"error":"removed_pending_rebuild","hint":"see docs/UPGRADE-PLAN.md"}` —
unchanged by this contract; they are not yet real answers.

---

## 4. `route_status` (typed empty results)

Present **only** when `results == []`, one of:

- `no_service` — the route/category has zero scheduled service from either
  carrier in the requested window (e.g. a seasonal seaside route queried in
  November). Not a failure; `sources` should show `ok` for providers that
  were successfully queried and simply returned nothing.
- `no_match` — service exists on the route, but nothing satisfies the
  request's constraints (budget, nights window, category filter). Also not
  a failure.
- `provider_error` — the emptiness is **not trustworthy**: a provider
  failed and no other provider could confirm "genuinely no service" vs.
  "we couldn't check". This pairs with exit code 1 (§ 3), never exit 0 —
  if `route_status` is `provider_error`, exit code MUST be 1.
  (`no_service`/`no_match` pair with exit code 0.)

`summary` must restate `route_status` in plain language — this is what
stops a watched seasonal route from reading as "broken" in a digest.

---

## 5. `deal_id` derivation

```python
import hashlib

def deal_id(origin: str, destination: str, out_date: str,
            return_date: str | None, shape: str, carriers: list[str]) -> str:
    key = "|".join([
        origin.upper(),
        destination.upper(),
        out_date,                        # "YYYY-MM-DD"
        return_date or "",                # "" for one-way, not the string "None"
        shape,                             # "S1".."S5"
        "+".join(sorted(c.lower() for c in carriers)),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
```

Decisions locked in:
- **Price is excluded** (per brief) so the id survives re-pricing across
  `check`/snapshot calls.
- **Field separator is `|`**, **carrier separator is `+`** (carriers are
  sorted first, so `["wizzair","ryanair"]` and `["ryanair","wizzair"]`
  produce the same id).
- **`return_date` uses `""`**, not the literal string `"None"` or `"null"`,
  when absent — avoids Python-vs-JSON `None`/`null` stringification
  ambiguity between implementations.
- **10 hex characters** (40 bits) — collision risk is irrelevant at this
  dataset size (tens of thousands of route/date/shape combinations, not
  billions); short ids are what `check <deal_id>` typing/pasting is for.
- Hash input is UTF-8 encoded before hashing.

---

## 6. `plan` output shape

`flight-deals plan --spec <file|-'{json}'>` compiles a search spec
(`SEARCH-DESIGN.md` §4) into a call plan **without touching the network**:

```jsonc
{
  "calls": [
    {
      "provider": "ryanair",
      "endpoint": "roundTripFares",
      "mode": "anywhere",           // "anywhere" | "exact" | "calendar" | "timetable"
      "shape": "S2",
      "params": { "origin": "BUD", "out_from": "2026-08-22", "out_to": "2026-08-24",
                  "duration_from": 5, "duration_to": 8 }
    },
    {
      "provider": "wizzair",
      "endpoint": "timetable",
      "mode": "timetable",
      "shape": "S2",
      "params": { "origin": "BUD", "destination": "CFU",
                  "date_from": "2026-08-01", "date_to": "2026-09-15" }
    }
  ],
  "estimated_calls": 33,
  "estimated_seconds": 45.0
}
```

When the `open-jaw` shape is compiled, the plan additionally carries
`openjaw_pairs_considered` and `openjaw_pairs_dropped` (both additive, Task 11):
the open-jaw pairs are capped at the 40 shortest-ground among the where-matched
airports, and the drop count makes any truncation visible rather than silent.
Both fields are absent on plans without the open-jaw shape.

- `calls`: ordered list of planned HTTP calls; each entry names the
  provider, endpoint, mode, the trip shape it serves, and the resolved
  params (post `--where` expansion — e.g. a category sweep is already
  flattened to concrete origins/destinations here, not left symbolic).
- `estimated_calls`: `len(calls)` — kept as an explicit field (not
  re-derived by the caller) so a `plan` diff (schema change, added
  confirmation calls) is visible without counting.
- `estimated_seconds`: wall-clock estimate assuming the shared rate
  limiter's configured rate (Global Constraint 9, default ~1 req/s) and
  warm cache for anything cacheable; used to warn on `--max-calls`-worthy
  specs before they run.
- `plan` never makes a network call; when the compiler itself hits a
  input error (bad spec), it follows the normal exit-2 contract (§ 3), not
  this shape.

---

## 7. Open items deliberately deferred (not decided here)

- ~~Exact reconciliation of `GroundLeg.estimated_cost_eur` vs. this doc's
  `legs[].cost_eur` naming.~~ **RESOLVED 2026-07-11 (Task 10):**
  `models.GroundLeg.estimated_cost_eur` was renamed to `cost_eur` — the name
  this contract froze for both the ground leg and the ground summary. The model
  keeps a `validation_alias` accepting the legacy `estimated_cost_eur` key on
  input, so older `data/ground_transfers.json` rows and embedded history dicts
  still load; the attribute and serialised key are now `cost_eur` everywhere.
- Golden per-intent-verb JSON outputs (mentioned in UPGRADE-PLAN §7 Phase
  0.5 item 3) are **not** produced by this task — they require the
  envelope to actually be emitted by code, which is Task 6's job (see this
  task's brief: "Out of scope: implementing the envelope in code"). Task 6
  should produce them from the fixtures captured here as its own first
  regression tests.
- `AVAIL` (booking/v4/availability, client-version fallback) has no Deal
  shape implications beyond filling in `departure_time`/`flight_number` on
  otherwise day-level legs — no separate contract needed.

---

## Changelog

- **2026-07-12 (Task 15)** — Gem onward-extension; additive, no frozen field
  changed shape:
  - **`deal_id` (§ 5)** gains an APPEND-ONLY `"|gem:<slug>"` component when a
    deal carries an onward gem extension (the gateway flight PLUS the onward
    ferry/bus/train chain). Absent otherwise, so every existing id is
    byte-identical; a gem-extended deal never collides with the plain gateway
    deal it was built from. Golden vector: `deal_id("BUD","RHO","2026-08-23",
    "2026-08-29","S2",["ryanair"], gem_slug="halki") == "d78e104b78"` (vs the
    plain `"0c2911c971"`).
  - **Deal object (§ 2)** gains two additive, optional fields, present ONLY on a
    gem-extended deal (absent — not null — elsewhere, so non-gem deals stay
    byte-identical):
    - `onward`: `{ gem, name, legs[], cost_eur, minutes, note, round_trip,
      season?, has_ferry?, marginal? }`. `legs` reuse the ground-leg dict shape
      (§ 2, `type:"ground"`, modes incl. `taxi`/`ferry`) and are the ONE-WAY
      chain; `cost_eur`/`minutes` are the shape-adjusted totals (×2 for a
      round-trip S2/S3, ×1 for a one-way S1). `has_ferry` true when a hop
      crosses water (the `why` then leads that hop with ⛴). All onward costs
      are curated estimates — the `why` marks them `~`.
    - `destination_display`: e.g. `"Halki (via RHO)"` — the human label; the
      Deal's `destination` stays the gateway IATA (`RHO`), and `shape` stays
      S1/S2/S3 (a gem is a terminal EXTENSION, not a new shape). S4 open-jaw is
      NOT gem-extended in v1 (return-routing ambiguity — documented scope cut).
  - Ranking, budget filtering, and watch/alert thresholds all operate on the
    EXTENDED total `price_eur` (fare + onward), the same ground-inclusive
    precedent as S3/S4 — no alert-machine change. History enrichment treats a
    gem deal like a composite (baseline group, non-comparative `why`, since its
    total has no direct-route history to compare against).
- **2026-07-12 (Task 14)** — City-anchor hybrid transit refinement; additive,
  no frozen field changed shape:
  - `estimate_basis` gains an additive enum value `"scheduled-hybrid"` (§ 2) — a
    computed open-jaw pair whose PURE airport-anchor query found no coverage but
    whose CITY-CENTER→CITY-CENTER line-haul is a real Transitous scheduled
    itinerary, with modeled airport-access pads added on each end
    (`hybrid_minutes = pad_a + line-haul + pad_b`). Because the pads are
    modeled, the `why` clause KEEPS `~` on the duration (unlike pure
    `"scheduled"`) and says "line-haul scheduled" (`"~3h45m line-haul
    scheduled, ~€25"`). Cost stays modeled (`~`). Precedence:
    `scheduled > scheduled-hybrid > modeled`.
  - Deal `ground` summary's additive `transit_transfers` (§ 2) now also appears
    on a `"scheduled-hybrid"` hop (the line-haul itinerary's transfers). Absent
    otherwise, so non-scheduled deals stay byte-identical.
  - `data/destinations.json` airports gain additive `city_lat`/`city_lon`
    (curated city-center anchors, shared across a multi-airport city) and, for
    notoriously-far airports, `access_pad_minutes` (default 30). No geocoding
    API. `schema_version` stays `2` (additive fields only).
  - `data/ground_matrix.json` computed pairs gain additive
    `transit_hybrid_minutes`/`transit_hybrid_transfers`/`transit_hybrid_modes`/
    `transit_hybrid_queried_at` + raw `linehaul_minutes`, written by
    `scripts/refresh_ground.py --transit` (the hybrid pass runs after the pure
    pass, on its `no_coverage` pairs). Same read-path acceptance bounds
    [0.5×, 3.0×] and 330/420 caps as the pure pass (the pads make the hybrid
    value structurally comparable to the airport-to-airport baseline).
    Coverage audit: `.orchestrate/task-14-report.md`.

- **2026-07-12 (Task 13)** — Transitous/MOTIS scheduled-transit refinement;
  additive, no frozen field changed shape:
  - `estimate_basis` gains an additive enum value `"scheduled"` (§ 2) — a
    computed open-jaw pair whose modeled duration was refined by a real
    Transitous scheduled itinerary (where coverage exists). Fares stay modeled
    (Transitous has no fares), so the `why` clause drops the `~` on the ground
    **duration** but keeps `~` on the **cost** (`"3h48 scheduled, ~€30"`).
  - Deal `ground` summary gains an additive `transit_transfers` (§ 2) on a
    scheduled hop only. Absent otherwise, so non-scheduled deals stay
    byte-identical.
  - `data/ground_matrix.json` computed pairs gain additive `transit_minutes`/
    `transit_transfers`/`transit_modes`/`transit_queried_at` (or `transit:
    "no_coverage"`), written by `scripts/refresh_ground.py --transit` (manual
    only). The read-path acceptance rule surfaces the scheduled minutes IFF
    within [0.5×, 3.0×] of the modeled value (else `transit_suspect`, modeled
    kept); the same 330/420 caps apply. `schema_version` stays `1` (additive
    fields only, precedent: `airports_seen`). Coverage is best-effort and
    sparse (see `.orchestrate/task-13-report.md`); the OSRM baseline is never
    blocked and a whole-service failure never invalidates the matrix.

- **2026-07-12 (Task 12)** — Ferry-aware ground modeling; additive, no frozen
  field changed shape:
  - Deal `ground` summary gains an optional `has_ferry: true` (§ 2) when the
    ground hop crosses water; the `why` string leads the hop with ⛴. Absent
    (never `false`) otherwise, so non-ferry deals stay byte-identical.
  - Five ferry corridors curated into `data/destinations.json open_jaw_pairs`
    (CTA↔MLA, HER↔JTR, KLX↔ZTH, CFU↔PVK, CTA↔SUF) with real ferry figures
    (`mode` `"ferry"`/`"ferry+ground"`) — curated wins over the computed pair.
  - `data/ground_matrix.json` computed pairs that the OSRM `/route` pass finds
    to cross water carry additive `has_ferry`/`ferry_minutes`/`land_minutes`/
    `sea_km` and `mode: "ferry+ground"` (a tiered ferry estimate, cap 420 min);
    a failed route pass records `has_ferry: null` rather than a false land pair.

- **2026-07-12 (Task 11)** — Computed ground matrix (open-jaw for any nearby
  registry pair); additive, no frozen field changed shape:
  - Deal `ground` summary gains an optional `estimate_basis` (`"curated"` |
    `"computed"`, § 2) — provenance for the ground hop. Absent when no
    provenance is attached, so deals without it stay byte-identical.
  - `plan` output gains optional `openjaw_pairs_considered` /
    `openjaw_pairs_dropped` (§ 6), present only when the `open-jaw` shape is
    compiled: open-jaw pairs are capped at the 40 shortest-ground among matched
    airports and the drop count is reported (no silent truncation).
  - New precomputed data file `data/ground_matrix.json` (refreshed out-of-band
    by `scripts/refresh_ground.py` via OSRM; never read from the request path).
    `registry.get_open_jaw_pairs()` merges it with the curated pairs — curated
    always wins on a shared `{a, b}` combo; tolerant when the file is absent.

- **2026-07-11 (Task 10)** — Trip shapes S3 (extended-origin) and S4 (open-jaw)
  enabled; additive, no frozen field changed shape:
  - New `shape` values `S3`/`S4` now appear in `results` (§ 2a documents their
    `legs`/`ground`/endpoint semantics). `S3.destination` is the flown airport;
    `S4.destination` is the fly-in airport D1 (fly-home D2 lives in `legs`).
  - `GroundLeg.estimated_cost_eur` renamed to `cost_eur` (§ 7 open item
    RESOLVED); legacy key still accepted on input via a validation alias.
    Ground `legs[].distance_km` is now nullable (a static curated hop has none).
  - `getaway` gains `--shapes` (comma list; default `direct`, NOT
    default-enabled). `via-hub` (S5) is still refused by the planner with a hint.

- **2026-07-11 (Task 8 fix wave 2)** — Reliability-backbone hardening; additive
  only, no frozen field changed shape:
  - `brief`'s envelope `brief` object gains an **optional** `searches_skipped`
    list (`{file, reason}`) — a saved search that fails to load (corrupt YAML,
    bad `schema_version`) or has a malformed `schedule` string is now skipped
    with a warning and surfaced here, never aborting the loop. Present only when
    at least one search was skipped; a run's exit code is unaffected by a skip.
  - Acknowledged-send ordering: a `brief --send` now fires alerts as *pending*,
    persists state, then sends; state is flipped to delivered ONLY on a
    confirmed send. A failed/transient Telegram send still exits 1 (unchanged)
    but the alert is re-included and re-sent on the next run instead of being
    silently suppressed — at-least-once on the wire, exactly-once on state (a
    crash between send and ack double-sends at most once). This adds an internal
    `sent: bool` field to `data/alert_state.json` entries (schema_version
    unchanged; legacy entries without it are treated as already-sent).
  when a search failed to even run, but ALSO when ≥1 search executed and every
  executed search had zero ok sources (all-providers-down day; envelope then
  carries `error=provider_error` + `hint`) — a quiet day (sources ok, no
  alerts) is unaffected and stays exit 0.

- **2026-07-11 (Task 8)** — Additive, backward-compatible extensions for the
  monitoring loop; no frozen field changed shape:
  - `SearchSpec` gains an **optional** `destinations` list (upper-case IATAs) so
    a single-route watch (`BUD-CFU`) can pin the planner to one route. Absent on
    every existing spec, so category plans/envelopes stay byte-identical.
  - `brief`'s envelope carries an extra top-level **`brief`** object
    (`searches_due`, `searches_ran`, `alerts`, `movers`) alongside the standard
    envelope fields — a convenience summary for the digest, ignorable by any
    consumer that doesn't know it. `results` on a brief run = alerting deals +
    top movers; `summary` is the digest sentence.

- **2026-07-10 (Task 7)** — Two **additive, optional** Deal fields (present
  only on the intent verbs `getaway`/`oneway`, never on the deterministic
  `run`/`plan` path, so existing envelopes stay byte-identical):
  - `estimated_price_eur` — retained pre-confirmation windowed estimate when an
    approximate (Wizz) fare was refined by an exact-date re-query
    (estimate→confirm, § UPGRADE-PLAN §4). `price_eur` then holds the confirmed
    figure; `price_confidence` is unchanged (a Wizz deal stays `approximate`).
  - `group` — `standout | solid | baseline` (SEARCH-DESIGN §2), attached by
    history enrichment. Absent when no enrichment ran.
  Neither repurposes a frozen field; consumers that don't know them ignore them.

- **2026-07-10** — Narrowed exit 1 (§ 3) from "`results` is `[]` or
  partial" to "`results` is `[]` **only**". A provider failing while
  another provider still returns ≥1 usable result is now exit 0, with the
  failure surfaced via `sources` (real status, never `ok`) and via a
  coverage-gap sentence appended to `summary`. Reason: cron/agent
  ergonomics — exit 0 means "usable results exist, act on them"; `sources`
  is where provider health lives, not the exit code. A non-zero exit on a
  partially-successful run would make cron treat a usable answer as a
  failure and would train agents to distrust/ignore non-empty `results`.
