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
- `error` / `hint`: present together, only when exit code is 1 or 2. `hint`
  is not generic advice — it is either an exact corrected command (exit 2)
  or a plain statement of what will happen automatically (exit 1, e.g. "the
  next scheduled run will retry").

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
    (`"driving"|"public_transit"|"train"|...`), `duration_minutes`,
    `distance_km`, `cost_eur` (nullable — estimate), matching the existing
    `GroundLeg` model in `models.py` (renamed `estimated_cost_eur` ->
    `cost_eur` for brevity is NOT done here; Task 6/7 reconciles model
    field names with this contract — see open item in § 7).
- **`ground`**: `null` for `S1`/`S2`. For `S3`/`S4`/`S5`, a **summary**
  object so a consumer doesn't have to walk `legs` to answer "how much
  ground transfer is involved":
  ```jsonc
  { "duration_minutes": 150, "cost_eur": 12.0, "mode": "driving" }
  ```
  This is a convenience mirror of the ground leg(s) already present in
  `legs`, not new data.
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

---

## 3. Exit codes

| Code | Meaning | `results` | `error`/`hint` |
|---|---|---|---|
| 0 | OK — command completed and the envelope is trustworthy, **including when `results` is `[]`** (a typed empty state, § 4, is a successful answer) | `[]` or populated | absent |
| 1 | Transient/provider failure — at least one provider needed to answer this query failed/blocked/parse-errored and no other provider could fully cover for it, so `results` may be incomplete; the caller should not conclude "no deals exist", only "this run couldn't tell". Retry = the next scheduled run (cron) or a manual re-run | `[]` or partial | present |
| 2 | Input error — bad IATA, unparsable dates, invalid `--where` expression, invalid flag combination; caught **before** any network call | `[]` | present, `hint` is an exact corrected command |

Rule of thumb: exit 1 is "the world didn't cooperate", exit 2 is "the
request was malformed" (agent's mistake, fixable without retrying
anything). A provider being down never becomes exit 2, and a bad flag never
becomes exit 1.

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

- Exact reconciliation of `GroundLeg.estimated_cost_eur` (models.py, Task 1
  baseline) vs. this doc's `legs[].cost_eur` naming — Task 6/7, whichever
  touches `models.py` first, aligns the two (rename in `models.py` to
  match this contract; this contract is authoritative, not the current
  `models.py`).
- Golden per-intent-verb JSON outputs (mentioned in UPGRADE-PLAN §7 Phase
  0.5 item 3) are **not** produced by this task — they require the
  envelope to actually be emitted by code, which is Task 6's job (see this
  task's brief: "Out of scope: implementing the envelope in code"). Task 6
  should produce them from the fixtures captured here as its own first
  regression tests.
- `AVAIL` (booking/v4/availability, client-version fallback) has no Deal
  shape implications beyond filling in `departure_time`/`flight_number` on
  otherwise day-level legs — no separate contract needed.
