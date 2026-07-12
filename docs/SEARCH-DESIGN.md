# Search Architecture — Design (v1, 2026-07)

**The thesis**: a deal-finder for Europe wins not by having one clever search,
but by cheaply enumerating a large *combination space* (origins × destinations ×
dates × trip shapes) and being honest about what it found. The endpoints we have
make bulk enumeration nearly free for Ryanair and cheap for Wizz — the missing
pieces are a **planner** that compiles a declarative search spec into the right
batch of calls, **trip-shape combinators** (via-hub, open-jaw, extended origin)
that run locally on already-fetched data, and a **category algebra** so "seaside
or italian" is a first-class query. Agents steer the spec; the engine owns the
procedure.

Companion to `UPGRADE-PLAN.md` (which owns phases/testing/state); this doc owns
the search model.

---

## 1. What the endpoints actually give us (capability inventory)

| Primitive | Endpoint | One call buys | Granularity | Confidence |
|---|---|---|---|---|
| RT-ANYWHERE | Ryanair `roundTripFares` (no arrival airport) | cheapest **paired** round-trip per destination from an origin, within an outbound window + `durationFrom/To` nights | per-destination best | exact |
| RT-EXACT | Ryanair `roundTripFares` (with arrival) | cheapest pairs for one route, incl. flight numbers + departure times | per-pair, with times | exact |
| CAL | Ryanair `oneWayFares/{O}/{D}/cheapestPerDay` | a month of one-way daily minima for one route+direction | per-day | exact per leg |
| OW-ANYWHERE | Ryanair `oneWayFares` (anywhere) | cheapest one-way per destination in a window | per-destination | exact |
| TT | Wizz `search/timetable` | daily minima **both directions** for one route over a date range | per-day | **approximate ±10%** |
| AVAIL | Ryanair `booking/v4/availability` (client-version fallback) | exact flights, times, fare buckets for one route+date | per-flight | exact, fragile |
| GROUND | local `ground.py` + OSRM/static | duration/cost between airports/cities | — | estimate |

Two structural facts drive the whole design:

1. **RT-ANYWHERE is a category sweep in ONE call.** "All Ryanair destinations
   from BUD, departing Aug 22-24, 5-8 nights, cheapest pair each" = one HTTP
   request. Enumeration is free on the Ryanair side; the art is filtering
   (category algebra) and enriching (Wizz, shapes, history).
2. **Day-level data is cheap; time-level data is expensive.** CAL/TT give
   prices per day but no flight times. So all combination logic (via-hub,
   open-jaw) runs **day-level first** on bulk data, and only a shortlisted
   handful get time-verified (RT-EXACT/AVAIL) before display or alert. This
   two-stage funnel — *enumerate wide at day level, confirm narrow at time
   level* — is the engine's core discipline and maps exactly onto the
   estimate→confirm alert pipeline in UPGRADE-PLAN §4.

## 2. The combination matrix (trip shapes)

What "a deal" can be, ordered by implementation cost. Every shape reduces to
primitive calls + local combination:

| # | Shape | Composition | Data needed | Notes |
|---|---|---|---|---|
| S1 | Direct one-way | OW-ANYWHERE / CAL | day-level | trivial |
| S2 | Direct round-trip | RT-ANYWHERE / RT-EXACT + TT pairing | day-level, exact for FR | the core product |
| S3 | **Extended origin** | GROUND(BUD→VIE/BTS) + S1/S2 from there | day-level | Vienna & Bratislava are 2-3h ground from Budapest and have huge FR/W6 bases — this alone is a major deal unlock, and it's just "run the same sweep from 3 origins and add ground cost" |
| S4 | **Open-jaw** | CAL(O→D1) × CAL(D2→O) × GROUND(D1↔D2) | day-level only | fly into Naples, train to Bari, fly home from Bari. Pure local combination over calendars we already fetch; no times needed (different days). Perfect for italian/spanish/croatian clusters |
| S5 | **Self-transfer via hub** (the "BUD→VIE then onward" pattern) | leg1 CAL/TT (O→H) × leg2 OW-ANYWHERE from H (× same for return) | day-level to enumerate, **time-level to confirm** (RT-EXACT/AVAIL for both legs, MCT ≥ 3h same-airport, separate tickets) | biggest search-space win, highest care: risk surfaced explicitly, generous buffers, day-level candidates marked `unverified_connection` until time-checked |
| S6 | Multi-stop nomad (3+ legs) | recursive S5 | — | **out of scope**; the spec format shouldn't preclude it, the planner refuses it |

Ranking across shapes: `total_price` = fares + ground estimate (+ a fixed
self-transfer risk buffer, configurable, default €25 displayed not hidden);
`value` = price vs route's historical percentile; `convenience` penalty for
ground time, transfer count, unsociable hours. Output groups results as
**standout** (≥25% below typical for that route), **solid**, **baseline** —
with a `why` string per deal ("€89 vs typical €140, 36% below, 42 observations")
so agents can narrate honestly.

## 3. Category algebra (the `--where` language)

Categories are not a flat enum — the user thinks in combinations ("seaside, or
italian, or spanish, or both, or mountains"). Model them as **tag expressions**
over the airport registry:

```
--where "seaside & (italy | spain)"      # italian or spanish seaside
--where "island & !canaries"             # islands but not Canaries
--where "mountains | lakes"              # alpine trip
--where seaside                          # bare tag
```

Grammar: `&` (and), `|` (or), `!` (not), parentheses, bare tag = itself.
Aliases expand before evaluation (`canaries` → `island & spain & winter-sun`).

**Tag taxonomy** (each airport in `destinations.json` gets several, one audit
pass to assign; ~15 min of curation beats any clever inference):

- **Country/region**: `italy, spain, greece, portugal, croatia, france,
  germany, poland, balkans, benelux, uk, scandinavia, morocco, canaries,
  baleares, sicily, sardinia, crete, cyclades…` (region tags where an island
  group is the real unit)
- **Terrain**: `seaside, island, mountains, lakes, thermal`
- **Vibe**: `city-break, party, quiet, hidden-gem, shopping, family, culture,
  hiking, winter-sun, ski`
- **Practical**: `seasonal-summer, seasonal-winter` (service seasonality),
  `wizz-served, ryanair-served, hub` (auto-derived from route data, not
  hand-curated)

`flight-deals where list` prints tags + counts; `where show "<expr>"` prints
the matching airports — the agent's sanity check before a big sweep, and the
answer to "what does 'mountainside' mean to this tool".

**Registry enrichment** (fine-tuning what exists): the current 58 airports get
a tag audit + seasonality + per-carrier service flags; add the Ryanair/Wizz
**route network** (fetched once per week from their public route endpoints,
cached) so the planner knows what's actually flyable from each origin/hub —
today's registry guesses, which is why sweeps waste calls on unserved routes.
Extend `ground_transfers.json` with BUD↔VIE, BUD↔BTS, and the open-jaw city
pairs inside each category cluster (NAP↔BRI, BCN↔VLC, ATH↔SKG…).

**Computed ground matrix** (Task 11 — open-jaw for _any_ nearby registry pair,
not just the 6 curated clusters): `data/ground_matrix.json`, precomputed
out-of-band by `scripts/refresh_ground.py` and read (never fetched) by the
registry + planner. The script haversine-prefilters registry pairs (straight
line ≤ 400 km, excluding same airport and same `multi_city` group), makes **one**
OSRM public `/table` request (`router.project-osrm.org`, driving profile,
`annotations=duration,distance`) for the full airport coordinate set, and applies
a stated estimate model:

```
ground_minutes = round(drive_minutes × 1.35 + 30)   # transit factor + airport-access pad
est_cost_eur   = max(8, round(km_road × 0.11))        # ~0.11 EUR/km, 8 EUR floor
```

keeping only pairs with `ground_minutes ≤ 330`. A road-sanity guard drops
disconnected-component pairs (e.g. separate Canary islands OSRM snaps to a
degenerate ~0 route) so nothing fabricates a "30-min hop across open sea". These
are **estimates, not fake precision** — the envelope marks them `~` and
`estimate_basis:"computed"`. The 6 curated pairs always win on merge
(`estimate_basis:"curated"`, exact hand-verified values). The planner considers
the 40 shortest-ground matched pairs per run and reports any dropped count.
**Refresh cadence**: monthly, or after any registry change that adds/moves
airports (see `docs/OPERATIONS.md`). **Follow-up (out of scope here)**:
Transitous/MOTIS could later replace the driving-derived estimate with real
public-transport timetables + fares for the kept pairs.

## 4. The search spec — one artifact for agents, cron, and humans

Everything above meets in a single declarative object. Agents produce it, saved
searches store it, the planner compiles it, `brief` diffs it. **This is the
API surface that matters**; CLI flags are just sugar for building one.

```yaml
# data/searches/august-seaside.yaml
schema_version: 1
name: august-seaside
spec:
  origins: [BUD]                 # or [BUD, VIE, BTS] / budapest-region alias
  where: "seaside & (italy | spain | greece)"
  depart: 2026-08-22..2026-08-24 # window | month (2026-08) | list of dates
  nights: 5-8                    # omit for one-way
  shapes: [direct, extended-origin, open-jaw]   # S2-S4; via-hub opt-in
  via: auto                      # for via-hub: auto (top hubs by coverage) | [VIE, BGY] | none
  budget: 180                    # EUR, total per person, incl. ground estimate
  carriers: [ryanair, wizzair]
schedule: "daily 08:30"          # optional → brief picks it up
alert:
  max_price: 150                 # confirmed-exact threshold
  notify: telegram
agent_prompt: |                  # optional → agentic review (see §6)
  Weekly: look at the results, try one creative variation (shift the window
  ±3 days, or swap greece for croatia), and message me only if you find
  something ≥25% below typical.
```

**CLI layers on top of the spec:**

```bash
# Layer 3 — INTENTS (what Hermes runs; compile to a spec internally)
flight-deals getaway --depart 2026-08-22..24 --where "seaside|italian" --nights 5-8 [--budget 180] [--shapes +via]
flight-deals oneway  --depart … --where …
flight-deals watch add <spec-ish flags or --spec file>   # watch = saved search + alert block
flight-deals brief [--send]                              # runs due saved searches, diffs, alerts
flight-deals check <deal_id>

# Layer 2 — SPEC (strong agents, saved searches)
flight-deals plan --spec <file|-'{json}'>   # compile only: shows the call plan,
                                            # estimated #calls and seconds, NO network
flight-deals run  --spec <file|-> [--max-calls 40]
flight-deals searches list|add|rm|show|due
flight-deals wake <name>                    # bundle spec + agent_prompt + last results
                                            # + history context, for an agent session

# Layer 1 — PRIMITIVES (plumbing; exploration by strong agents)
flight-deals fares rt <O> [<D>|--anywhere] --out 2026-08-22..24 [--nights 5-8]
flight-deals fares calendar <O> <D> --month 2026-08
flight-deals fares timetable <O> <D> --range 2026-08-01..2026-09-15
flight-deals where list|show "<expr>"
flight-deals routes <airport>               # what's flyable from here (cached network)
```

**The planner** (deterministic query compiler) turns a spec into a call plan:

```
spec{origins:[BUD], where: seaside&(italy|spain|greece), depart: Aug22-24, nights:5-8, shapes:[direct,extended-origin,open-jaw]}
  └─ resolve where-expr → 23 airports (where show output embedded)
  └─ RT-ANYWHERE BUD (out Aug22-24, dur 5-8)              1 call   [S2 FR]
  └─ TT BUD↔{12 Wizz-served of the 23}                   12 calls  [S2 W6]
  └─ RT-ANYWHERE VIE, BTS                                 2 calls  [S3 FR]
  └─ CAL pairs for open-jaw clusters (NAP/BRI, BCN/VLC…) 10 calls  [S4]
  └─ combine + rank locally                               0 calls
  └─ RT-EXACT confirm top 8                               8 calls
  ≈ 33 calls, ~45s warm-cache, honest per-source status
```

`plan` prints exactly this (JSON + pretty) — so an agent (or you) can inspect
cost before running, and `--max-calls` caps runaway specs. Every run records
the plan + results snapshot, which is what `brief` diffs next time.

**Why this shape**: determinism where it matters (same spec → same procedure →
comparable results over time, which is what makes price history and alerting
meaningful), agent creativity where it helps (specs are cheap to vary — an
agent exploring "what if Croatia" mutates one line and reruns `plan` first).

## 5. Steering: how agents of different strengths drive it

**Weak model (Hermes on Grok-class) — intents only.** The SKILL.md router maps
utterance → one Layer-3 command. The `next` field in every response offers at
most ONE widening move (e.g. "0 results under budget; nearest €176 — rerun
with --budget 190 or --depart 2026-08-20..27"). Rule in skill: *follow `next`
at most twice, then report what you have.* No spec authoring, no primitives.

**Strong model (Claude Code, Opus-class Hermes) — spec layer.** May author
specs, use `plan` to sanity-check cost, use primitives + `where show` +
`routes` for exploration, and create/modify saved searches. The skill's
`references/spec-guide.md` (one level deep) documents the spec schema with
five worked examples, including the failure modes ("via-hub without `nights`
explodes the space — the planner will refuse; set nights or shapes:[direct]").

**The worked example the skill leads with** (the user's own scenario):

> *"Best deals departing Aug 22-24, seaside or italian or spanish, about a week"*
> → `flight-deals getaway --depart 2026-08-22..2026-08-24 --where "seaside | italy | spain" --nights 5-8`
> Agent's job: pick the verb, translate dates and category words to the
> `--where` expression (`where list` if unsure), relay `summary`, offer the
> `next` options. The engine's job: everything else — which airports, which
> calls, pairing, ranking, confirming.

## 6. Scheduled search: deterministic core + agentic periphery

Two loops, cleanly separated:

1. **Deterministic (cron/launchd)**: `brief --send` runs every saved search
   whose `schedule` is due — compiled plan, diff vs last run + history,
   alert-state machine, exactly-once Telegram alerts. No model in the loop;
   this is the reliability backbone. (UPGRADE-PLAN §6 owns the mechanics.)
2. **Agentic (periodic, cheap-to-skip)**: saved searches with an
   `agent_prompt` also appear in `flight-deals searches due --agentic`. A
   scheduled Hermes/Claude session runs `flight-deals wake <name>` and gets one
   self-contained bundle: the spec, the prompt, last results, history context,
   and the allowed follow-up moves. The agent *reasons* — tries one variation,
   compares, decides whether anything is worth a message — then optionally
   updates the saved search (`searches add --replace`). Its creativity is
   sandboxed to spec mutations + a messaging decision, so a weak week just
   means "no news", never a broken pipeline.

This is the answer to "utilize the agent's brain with enough determinism": the
engine never needs the agent (alerts flow regardless), and the agent never
fights the engine (its output is a spec, which compiles deterministically).

## 7. What this changes in UPGRADE-PLAN.md

- **Phase 1 additions**: fetch + cache the Ryanair/Wizz route networks;
  registry tag audit + seasonality + service flags; ground pairs for BUD↔VIE/BTS
  and open-jaw clusters.
- **Phase 2 becomes spec-centric**: the planner + spec schema + `plan`/`run`
  land here; intent verbs are thin spec builders. (JSON envelope from Phase 0.5
  unchanged — `plan` output joins the golden fixtures.)
- **Phase 3 additions**: saved searches (`data/searches/*.yaml`) are the watch
  mechanism (a "route watch" is just a one-route spec); `brief` = run-due +
  diff + alert.
- **Phase 4 additions**: skill router gains the `--where` translation table and
  the two-follow-ups rule; `references/spec-guide.md` for strong agents;
  `wake`/`searches due --agentic` for scheduled agentic review.
- **Phase 5 becomes the shapes ladder**: 5a extended-origin (S3, trivial),
  5b open-jaw (S4, local combination + ground), 5c via-hub self-transfer (S5,
  needs the time-verification funnel via RT-EXACT/AVAIL and explicit risk
  presentation). Ordering by cost/risk; each shape ships only with its
  confirm-before-alert path.
- **Category-watch call math** (corrects v2 §4): a category watch is 1
  RT-ANYWHERE call per origin (not N calendars) + per-route TT for Wizz-served
  matches; CAL calendars are reserved for single-route watches and open-jaw.

## 7b. Ferry-aware ground modeling (designed 2026-07-12; Task 12 in the orchestration plan)

The computed ground matrix prices sea crossings with land math — wrong in both
dimensions (real ferry fares ≫ €0.11/km; sparse sailings mean waiting dominates,
not crossing time). The fix, in authority order:

1. **Detection**: a second OSRM `/route` pass (steps=true) per kept pair splits
   each route into `land_minutes` / `ferry_minutes` / `sea_km` via `mode=="ferry"`
   steps; island-region tags cross-check detection. Route-pass failure degrades
   to `has_ferry: null` + warning — never a silent false negative — UNLESS the
   pair is island-suspect, in which case it is dropped from the matrix
   entirely (a null land estimate for an unverified island crossing would be
   mispriced, not merely uncertain).
2. **Curated corridors win** (existing mechanism): routes that matter get
   hand-curated real figures (CTA↔MLA Virtu Ferries, HER↔JTR SeaJets/Blue Star).
3. **Ferry model for the rest** (REVISED to a TIERED model 2026-07-12 — a sea
   crossing is not a road: real fares ≫ €0.11/km and sparse sailings mean the
   WAIT dominates, so the pads scale with `sea_km` as a sailing-frequency
   proxy). Time = `land×1.35 + ferry×1.15 + port_access + sailing_wait`; cost =
   `max(8, land_km×0.11) + base + sea_km×rate`. Tiers by `sea_km`
   (wait / base / rate / port), CALIBRATED against the five curated corridors:
   - **strait** (`<15 km`, turn-up-and-go): 5 / €5 / 0.15 / 10
   - **domestic** (`15–60 km`, a few/day): 30 / €5 / 0.15 / 30
   - **long** (`≥60 km`, 2–3/day): 110 / €35 / 0.15 / 45

   Cap 420 min (land keeps 330); mode `ferry+ground`; ⛴ in why-strings; additive
   `has_ferry` in the envelope so agents disclose the crossing before the user
   gets attached to a price. Calibration: modeled DURATION is within ±40% of all
   five curated corridors and COST within ±40% of four — CTA↔SUF cost is a
   documented outlier (a genuine ~228 km road leg at the shared €0.11/km land
   proxy ≈ €25 vs an atypically cheap curated €15; curated wins, so unseen).
   A failed `/route` pass degrades to `has_ferry: null` — never a fabricated
   land pair. Tier constants live in `registry.ground_matrix.FERRY_TIERS`; the
   default constants over-shot the long-overland corridors, hence the tuning
   (recorded in `.orchestrate/task-12-report.md`). Because tiers select by a
   `sea_km` threshold rather than a continuous function, the estimate is a
   step function at each boundary (~+45min crossing 15km, ~+€30 base /
   ~+80min wait crossing 60km) — acceptable for a STATED ESTIMATE and
   documented deliberately, not tuned away.
4. **Transitous/MOTIS** (Task 13, as-built 2026-07-12): the
   `scripts/refresh_ground.py --transit` third pass (manual-only, after
   table+route) refines a kept pair's modeled duration with a real scheduled
   itinerary where the free Transitous API (MOTIS) has coverage. Recipe (live
   probed): `GET api.transitous.org/api/v1/plan?fromPlace=lat,lon&toPlace=lat,
   lon&time=…&numItineraries=3&transitModes=<ground modes>`, querying two
   representative departures (next Tuesday ≥14d, 10:00 & 15:00 UTC) and taking
   the shortest ground itinerary (`min(endTime−startTime)`). **AIRPLANE legs are
   excluded** — the load-bearing insight: airport-coordinate queries otherwise
   return absurd air "connections" (e.g. BUD→CAG→VIE), not the ground hop.
   Stored additively per pair (`transit_minutes`/`transit_transfers`/
   `transit_modes`/`transit_queried_at`, else `transit:"no_coverage"`). The
   read-path acceptance rule (`registry.ground_matrix.apply_transit_refinement`)
   surfaces the scheduled minutes as `estimate_basis:"scheduled"` IFF within
   [0.5×, 3.0×] of the modeled value (outside → `transit_suspect`, modeled kept),
   with the same 330/420 land/ferry caps (a scheduled value over cap is an honest
   "too far", dropped at refresh). The 0.5× floor is a deliberate tradeoff: a
   legitimately fast train that comes in below half the modeled duration is
   rejected as suspect and the slower modeled value is kept instead — the safe
   direction (it never fabricates a too-good-to-be-true number) at the cost of
   occasionally under-crediting a real deal. Scheduled values get **no** wait pad
   (travellers plan around timetables) and lose the `~` on duration; fares stay
   modeled/curated (Transitous has no fares), keeping `~` on cost. **Coverage is
   sparse**: most registry airports have no on-site rail/bus stop in Transitous's
   aggregated feeds, so airport→airport ground routing returns nothing (the live
   run refined 2 of 38 computed pairs — AMS-CRL, CIA-NAP). Never blocks the OSRM
   baseline; a whole-service failure never invalidates the matrix. Full probe +
   coverage audit: `.orchestrate/task-13-report.md`.
5. **City-anchor hybrid** (Task 14, as-built 2026-07-12): the airport-anchor
   pure pass (item 4) hits a coverage ceiling because most airports have no
   on-site rail/bus stop in the feeds (only 2/38 refined). The `--transit`
   FOURTH pass raises coverage by re-querying the pairs the pure pass left at
   `no_coverage` from CITY-CENTER anchor → CITY-CENTER anchor (the intercity
   line-haul, which the feeds DO cover) and adding modeled **airport-access
   pads** on each end:

       transit_hybrid_minutes = pad_a + best_city_linehaul_minutes + pad_b

   The pads are included DELIBERATELY: the OSRM airport-to-airport baseline
   already embeds airport-side travel, so the hybrid must too, or the acceptance
   bounds ([0.5×, 3.0×] vs the modeled `ground_minutes`) and 330/420 caps would
   compare unlike things. Because pad + line-haul + pad IS structurally the same
   shape as the modeled minutes, the SAME bounds/caps hold. Pads: default
   30 min/airport, curated per-airport `access_pad_minutes` overrides for
   notoriously-far airports (BVA 75, STN 55, LTN 50, CRL 55, MXP 50, FCO/CIA 45,
   BGY/BUD 40, VIE/BER/MAD/BCN 35 — full table in `data/destinations.json`, one
   `access_pad_minutes` per airport). City anchors are curated `city_lat`/
   `city_lon` per airport (no geocoding API), shared across a multi-airport city
   (MXP+BGY → Milan, STN+LTN+LGW → London, FCO+CIA → Rome, CDG+BVA → Paris,
   CRL → Brussels). This is an HONEST HYBRID: the line-haul is real scheduled
   data, the access is modeled — so `estimate_basis` is `"scheduled-hybrid"`
   (NOT `"scheduled"`), the `why` clause KEEPS the `~` on duration and says
   "line-haul scheduled", and cost stays modeled. Precedence at read time:
   `scheduled > scheduled-hybrid > modeled`. Stored additively per pair
   (`transit_hybrid_minutes`/`transit_hybrid_transfers`/`transit_hybrid_modes`/
   `transit_hybrid_queried_at` + raw `linehaul_minutes`); the pure
   `transit:"no_coverage"` marker is left in place as a factual record. A hybrid
   value over cap is dropped at refresh (honest "too far"). Coverage audit:
   `.orchestrate/task-14-report.md`.

## 8. Open questions (decide during implementation, low stakes)

- Spec date DSL: support weekday patterns (`fri..sun of 2026-08`)? Start
  without; add if a saved search wants it.
- Hub auto-selection: static curated list (VIE, BTS, BER, MXP/BGY, FCO/CIA,
  BCN) vs computed from route-network connectivity × category coverage. Start
  static, compute later.
- Wizz via-hub legs: allow in S5 candidates but require both legs
  exact-confirmed before display, or restrict S5 to FR×FR initially? Start
  FR×FR + FR×W6-with-confirmation.
- Should `getaway` auto-include S3 (extended origins) by default once built?
  Probably yes with ground cost shown; flag `--from BUD-only` to disable.
