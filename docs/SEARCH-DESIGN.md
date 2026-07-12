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

## 2b. Gem destinations (as-built 2026-07-12; Task 15)

A **gem** is a curated non-airport place — a small island, mostly — reached via
a **gateway airport** plus an onward ferry/bus/train chain. A gem is a *terminal
extension* of an ordinary deal, **not a new trip shape**: the flight is still an
S1/S2/S3 to the gateway; the onward chain is appended after the fact using the
existing ground-leg machinery (the same way S3/S4 attach ground). Nothing about
fares, shapes, alerts, or `deal_id` for non-gem deals changes.

**Data** (`data/destinations.json` → `gems`, additive; `schema_version` stays 2):
each gem has `slug`, `name`, `country`, `tags` (existing taxonomy), and one or
more `gateways`, each `{airport, legs:[{mode, from, to, minutes, cost_eur}],
total_minutes, total_cost_eur, note, season?}`. A gem may carry a gem-level
`season` (a `"may-oct"` month window; a gateway's own `season` overrides it) and
`marginal: true` (day-trip / awkward-connection gems, curated but held back from
category matching). All curated; `estimate_basis` is implicitly `"curated"`;
totals are `~` estimates; ferry legs set `has_ferry`/⛴.

**Onward arithmetic by shape** (settled): S2/S3 round-trip = onward cost & minutes
**×2** (out AND back through the gateway); S1 one-way = **×1**; **S4 open-jaw is
NOT gem-extended in v1** (which of the two cities does the onward hang off? —
documented scope cut). Multi-gateway gems: a variant is built from each present
gateway; the **cheapest total per (gem, origin) survives** (dedupe).

**Matching — a deliberately separate seam from airport `matching()`.** The
airport tag-matcher stays airport-only and network-free; gems are matched by a
distinct `registry.gems_matching(expr, window)` that ALSO season-gates against
the search window. The two are kept apart on purpose:

- The deterministic `compile`/`plan` path is **untouched** by gems — it fans out
  Wizz TT per *airport-matched* destination exactly as before, so `plan` output
  (and its goldens) stay byte-identical and a plain search never burns extra
  calls for a gem's gateway.
- A where-matched gem instead contributes its gateway airports to the fare
  **filter** at execute-time (`planner.execute`) — so the single already-fetched
  Ryanair RT/OW-ANYWHERE payload surfaces a candidate for each gateway — and an
  **onward extension directive** at intent-time (`intents.execute_spec`, after
  confirm). No new planned calls; the gateway's exact Ryanair fare is free.

**Season gating.** A gem matches `--where` only when at least one gateway's
effective season (gateway season, else gem season, else year-round) overlaps the
`--depart` window; a wholly out-of-season gem is dropped. `--to <gem>` is
explicit and ignores season (the season is still surfaced in the `onward`
object).

**Reaching a gem.** `--where` matches KEEP gems by tag and shows **both** the
plain gateway deal and the gem-extended variant (the gateway airport is also
someone's plain destination). `--to <slug|name>` (via `resolve_gem`, typo-hinted)
restricts the sweep to the gem's gateways, forces the extension (marginal gems
included), and displays **only** the gem variants. Budget, rank, `max_results`,
and watch/alert thresholds all apply to the extended total (fare + onward),
mirroring the S3/S4 ground-inclusive precedent. `where show "<expr>"` lists
matched gems distinctly (marginal flagged).

**Out of scope (v1):** Azores (S5-dependent), pseudo-airports, live schedule
APIs, and any S5 self-transfer onward.

## 2c. Via-hub self-transfer (as-built 2026-07-12; Task 16)

S5 is the final shape: two SEPARATE same-day Ryanair tickets through a hub,
`O→H→D` out and `D→H→O` back. A missed connection is the traveller's own risk,
so it is held to the strictest honesty bar in the project.

**Key finding (our own fixtures):** farfnd legs carry BOTH `departureDate` AND
`arrivalDate` (incl. overnight, e.g. `21:40→00:05` next day). So the whole shape
runs on farfnd exact-date one-way queries — the hostile `booking/v4/availability`
endpoint is **not used at all**. The provider models gained additive
`departure_at`/`arrival_at` ISO strings (`DayFare`, `FareLeg`) carrying the full
airport-local datetime.

**Settled rulings, as built:**
- **FR×FR only** (Wizz timetable has no times). **Same-airport connections
  only** (no BGY→MXP metro hop) — which is exactly what makes the connect math
  correct on farfnd's airport-local naive datetimes: both instants are in hub
  H's local zone, so their delta is timezone-correct with no tz database, and an
  overnight arrival is handled because the delta is computed on full datetimes,
  never date arithmetic.
- **MCT**: `min_connect_minutes` ≤ gap ≤ `max_connect_minutes` (config, default
  180/480). Below the floor is unsafe; above the ceiling is a stopover. Both
  drop.
- **Two-stage funnel.** (1) DISCOVER: one OW-ANYWHERE sweep from the origin
  (filtered to hubs) + one per hub (filtered to where-matched destinations);
  compose same-day, MCT-plausible outbound pairs straight from that data.
  (2) VERIFY the cheapest 6 by composite price. The outbound (O→H, H→D on the
  out date) is fixed from discovery — that data already carries times and exact
  fares, and its connection was MCT-checked in stage 1, so it is reused as-is.
  The RETURN side is swept (Task 17, below). A candidate becomes a deal ONLY
  after both return legs book on the chosen date and both connections (the
  discovery-verified outbound + the freshly-verified return) pass MCT.
  **Unverified candidates are NEVER displayed or alerted** (hard rule) — an
  unbookable leg or a sub-MCT verified gap drops the candidate silently from the
  results (logged, not shown).
- **Return-window sweep (as-built 2026-07-13; Task 17)** — the yield fix. Per
  shortlisted candidate, instead of verifying one fixed return date
  (out+min-nights) on independently-cheapest legs: (a) 2 CAL (`cheapestPerDay`)
  calls per return month — D→H and H→O — over the nights window (capped at 2
  months; deduped by (o,d,month) per run, 6h calendar cache tier); (b) locally
  pick the cheapest return date inside the nights window where BOTH legs fly;
  (c) time-verify that date with 2 fresh exact-date `oneWayFares` calls, MCT
  [180,480] on hub-local datetimes; on an MCT/bookability failure, ONE retry on
  the next-best-priced date (2 more exact), then drop. Budget per candidate:
  2 CAL/month + 2 exact + 2 retry — reserved honestly in `estimated_calls` and
  the `--max-calls` account. The CAL day-level prices are *selection* minima
  only; the exact-verified fares REPLACE them in the total (still exact + buffer,
  no estimate leakage). The date-selection math is a pure, unit-tested function
  in `engine/via_hub.py` (`select_return_dates`); orchestration is in the planner.
- **Pricing**: `price_eur` = 4 leg fares + `self_transfer_buffer_eur` (default
  €25, DISPLAYED: "incl. ~€25 self-transfer buffer"). Ranked/budgeted on that
  total; `price_confidence: exact` once verified. Additive `connection`
  `{hub, connect_out_minutes, connect_ret_minutes, verified, separate_tickets,
  buffer_eur}`; `why`/`summary` carry the separate-tickets disclosure ALWAYS.
  A verified S5 may alert on its buffer-inclusive total.
- **Planner**: `compile` stays pure — hubs for `via:auto` are the registry
  `hub`-tagged airports statically pre-filtered by `KNOWN_DIRECT_ROUTES` (the
  true ryanair-served intersection is enforced by the discovery data at execute
  time). The plan reserves the verification ceiling (shortlist × 4) in
  `estimated_calls` so `--max-calls` budgets honestly; the funnel's execution
  lives in `planner._run_via_hub` (discovery + verification via the shared
  executor/token bucket), the pure MCT/composition half in `engine/via_hub.py`.
- **Scope note (bounded verification):** ~~v1 verifies each shortlisted
  candidate at a single return date = out + `min(nights)`.~~ **Superseded by the
  Task 17 return-window sweep (above):** the return date is now chosen by a CAL
  price sweep across the whole nights window (both legs must fly), then
  time-verified, with one retry on the next-best date. The outbound stays fixed
  from discovery.
- **Azores/PDL** (S5 follow-up — CLOSED 2026-07-13, Task 18): the registry now
  carries PDL (Ponta Delgada/São Miguel) + TER (Lajes/Terceira) with an `azores`
  region tag, and LIS + OPO joined `HUB_IATAS`, so a `where=azores --shapes
  via-hub` spec compiles self-transfer descriptors through Lisbon/Porto. **Live
  finding (routes() 2026-07-13): Ryanair does NOT serve the Azores.** LIS serves
  FNC (Madeira) but no PDL/TER; OPO serves only FAO/FNC; PDL/TER are not Ryanair
  airports at all (the Azores are TAP/SATA territory). Since S5 v1 is FR×FR only,
  **no verified S5 to the Azores can surface** — the hub fan-out honestly offers
  LIS/OPO but the onward leg simply doesn't exist in the Ryanair network (recorded
  in `tests/fixtures/ryanair_routes_lis.json` + pinned by a test). The unlock is
  data-complete and future-proof (a non-Ryanair provider or a TER+ferry gem would
  light it up); the S5-via-Ryanair path is a dead end by real-world route reality.

Live finding (2026-07-12 smoke, Task 16 baseline): verified-S5 yield was LOW with a single fixed return date — farfnd exposes only the cheapest fare per route per day, so verifying one date (out+min-nights) on independently-cheapest legs could only test whether those legs happen to connect; in the first live run 5/5 MCT-plausible outbound candidates verified but all 5 failed on the return connection (0-of-5). This was honest scarcity, not a defect — the funnel showed nothing rather than something unverified.

Amendment (2026-07-13, Task 17 shipped): the return-window sweep replaces the single fixed return date. Verification now searches the whole nights window for the cheapest return date on which BOTH legs fly before time-checking it (plus one retry), giving MCT a real chance to hold on a bookable date instead of gambling on out+min-nights alone. Live re-run yield delta is recorded in `.orchestrate/task-17-report.md`.

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
  static, compute later. **RESOLVED (Task 16):** `via:auto` uses the registry
  `hub`-tagged set (`HUB_IATAS`), pre-filtered per origin by the static
  `KNOWN_DIRECT_ROUTES` so `compile` stays pure; the real ryanair-served
  intersection is then enforced by the discovery sweep data at execute time (a
  hub with no live O→H fare yields no candidate). `via:["VIE",…]` overrides with
  an explicit list; `via:none` disables the fan-out. Route-network-derived
  connectivity remains the compute-later refinement.
- Wizz via-hub legs: allow in S5 candidates but require both legs
  exact-confirmed before display, or restrict S5 to FR×FR initially? Start
  FR×W6-with-confirmation. **RESOLVED (Task 16):** v1 is **FR×FR only** — the
  Wizz timetable carries no flight times, so it can't feed the MCT gate. A
  Wizz-leg S5 (times sourced elsewhere, both legs exact-confirmed) is the
  documented follow-up.
- Should `getaway` auto-include S3 (extended origins) by default once built?
  Probably yes with ground cost shown; flag `--from BUD-only` to disable.
