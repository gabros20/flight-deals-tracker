# Flight Deals Tracker — Upgrade Plan v2 (2026-07)

> **STATUS: COMPLETED 2026-07-13** — all phases and follow-ups shipped (18
> gated tasks, 560 tests green). This document is now a historical planning
> record; it is kept for the "why" behind decisions, not as current truth.
> For as-built behavior, read `docs/SEARCH-DESIGN.md` (search model, trip
> shapes, gems) and `docs/CONTRACT.md` (frozen output schema) instead.

**Goal**: Turn the tool into a solid, agent-first deal monitor: reliable round-trip
search, deterministic pipelines an agent (Hermes, any model tier) can drive without
inventing strategy, and a cron-safe watch/alert loop controlled from Telegram.

**v2 changes** (second pass, 2026-07-10): adversarial design review (22 findings)
applied; free-stack research folded in — the default stack is now **fully free**
(Apify demoted to optional insurance); new Phase 0.5 freezes the output contract
before any rebuild; currency, alert-state, concurrency, and testing are now
specified instead of hand-waved.

**v2.1** (third pass): the search model got its own design —
**`docs/SEARCH-DESIGN.md`** — covering the primitive inventory, trip shapes
(direct / extended-origin / open-jaw / via-hub), the `--where` category
algebra, the declarative search spec + planner (`plan`/`run`), and
deterministic-vs-agentic scheduled searches. Phases below are annotated with
its deltas; where the two docs overlap, SEARCH-DESIGN.md wins on search
semantics, this doc wins on state/testing/rollout.

Sources: full code audit, live endpoint verification (2026-07-04), data-source
survey, agent-ergonomics research (Anthropic tool/skill guidance), free-tier API
survey, OSS deal-bot ecosystem survey, and the Personal-OS design principles
(`~/Downloads/PERSONAL_OS_DESIGN.md`).

---

## 1. Diagnosis — why it feels flaky

### 1a. The data layer is mostly illusory

The audit found the tool is effectively a **single-provider, one-way search**:

| Component | Status | Root cause |
|---|---|---|
| Ryanair one-way search | ✅ works | — |
| Wizz provider | ❌ returns nothing, always | pasted block references undefined vars → NameError on every 200 response, swallowed (`wizz.py:68`) |
| Round-trip via farfnd | ❌ silently fails | strings passed where `date` objects expected; `.isoformat()` AttributeError swallowed (`orchestrator.py:60` → `ryanair_direct.py:35`); return window ignored |
| `search` with return dates | ❌ misleading | outputs reversed one-way legs (PMI→BUD) presented as trip results |
| `roundtrip` command | ❌ crashes | calls `orchestrator.find_roundtrip_deals` which doesn't exist |
| `--connections` composites | ❌ can't run | `_build_multi_airport_composites` never defined; AttributeError swallowed |
| Fake results | ❌ actively lying | hardcoded €61 "DEMO self-transfer" injected into BUD --connections results (`orchestrator.py:198-223`); `cron_report.py` prints a fabricated €117.64 deal when the API returns nothing |
| Apify provider | ❌ discards results | swapped cache args → exception → `[]` (`apify.py:115`) |
| `collect`, `alerts`, `history` CLI commands | ❌ NameError/AttributeError | undefined vars, missing model fields |
| Cron from any cwd | ❌ silent empty results | all data paths are cwd-relative; registry loads 0 airports |

**The meta-bug**: 17 bare `except: pass` blocks (9 in orchestrator alone) make
"No deals found" indistinguishable from six crash classes. Tests are smoke tests
(`isinstance(x, list)`) that can't tell "found deals" from "everything failed".

### 1b. The agent ergonomics are inverted

The pipeline lives in the model's head: a "creative usage" skill offering many
strategies and ~12 flags per command — a **high-freedom skill for a low-freedom
problem**, the exact anti-pattern Anthropic's skill guidance calls out for weaker
models. A Grok-4.3-class Hermes must *plan* (pick strategy, compose flags,
sequence commands) where it should only *follow*. No JSON output, no state, no
next-action hints, no corrective errors — and the tool sometimes lies (fake
deals, silent failures), so the agent can't trust what it sees.

---

## 2. Target architecture

```
┌─ Telegram (you, phone) ──────────────────────────────────────┐
│                    Hermes agent (any model)                  │
│   SKILL.md = thin intent router: "user wants X → run Y"      │
└───────────────┬──────────────────────────────────────────────┘
                │ same surface for human / cron / agent
┌───────────────▼──────────────────────────────────────────────┐
│  flight-deals CLI — intent verbs (pipelines live HERE)       │
│  getaway · watch add/list/rm · brief · check · search        │
│  JSON out (results + summary + sources + next) · exit 0/1/2  │
│  errors carry corrective commands · never interactive        │
├──────────────────────────────────────────────────────────────┤
│  Engine: date-grid planner, dedupe, ranking, alert state     │
│  machine, estimate→confirm pipeline, EUR normalization       │
│  State (git-friendly, schema_version'd, atomic writes):      │
│  watchlist.yaml · alert_state.json · price_history.csv ·     │
│  deal snapshots (append-only observations, stable IDs)       │
├──────────────────────────────────────────────────────────────┤
│  Providers — FREE BY DEFAULT, honest about failure:          │
│  • Ryanair farfnd: roundTripFares (+anywhere) + cheapestPer- │
│    Day calendars                    [exact prices]           │
│  • Wizz timetable (auto version re-discovery) [approximate]  │
│  • [opt, config-gated] Apify multi-source · SerpApi insights │
└──────────────────────────────────────────────────────────────┘
```

Design rules (Personal-OS + Anthropic tool guidance):
- **One command surface** — human, cron, Hermes run identical commands. No MCP needed (research verdict: output quality beats transport).
- **Pipelines in the tool, not the prompt.** One verb per intent; every flag has a smart default.
- **Honest output.** Per-provider status in every response. No fake data, ever. Empty results are structured success with a hint.
- **Errors are prompts**: exit 2 + `{"error", "message", "hint": "<exact corrected command>"}`.
- **One renderer**: JSON is canonical; `--pretty` and the Telegram digest are both generated from the same `summary`+`results` fields by one formatter module.
- **State as plaintext files**, git-versioned, `schema_version` field in every new format, atomic writes (tmp + `os.rename`).
- **Timestamps**: stored timestamps are timezone-aware UTC; user-facing dates are airport-local calendar dates; comparisons happen in UTC.
- **Secrets**: tokens come from env only (launchd plist supplies them via `op run --` or an EnvironmentVariables block). `config --set-telegram-token` is **removed**, not fixed.

---

## 3. Provider stack — fully free by default

The second research pass settled the Apify question. Findings:

1. **For LCCs, the airline's own price IS ground truth** — there is no GDS/OTA
   layer to reconcile against. The entire OSS deal-bot ecosystem alerts straight
   off farfnd/timetable data. Multi-source pre-alert verification is insurance
   against rare edge cases (sold-out promo buckets), not a load-bearing check.
2. **fast-flights (free Google Flights) is disqualified** for anything
   load-bearing: PyPI release broken (#109, #102), maintainer's own warning of
   week-to-year Google blocks (#55). Best-effort bonus signal at most; not v1.
3. **No recurring-free-tier competitor to SerpApi exists** (all others are
   one-time trial credits). And our **self-collected history is the right
   primary answer** to "is €150 good for this route" — LCC-specific, unlimited.
4. **Nobody has built local virtual interlining from airline calendars** — but
   we already fetch both carriers' one-way calendars free; combining them with
   MCT rules is ~150 lines we can own (Phase 5).

| Layer | Source | Cost | Confidence | Role |
|---|---|---|---|---|
| Ryanair search | `farfnd/v4/roundTripFares` (durationFrom/To; anywhere-mode without destination) | free | **exact** | primary round-trips + category sweeps |
| Ryanair monitor | `farfnd/v4/oneWayFares/{O}/{D}/cheapestPerDay?outboundMonthOfDate=` | free | exact per-leg | cron calendars: one call = a month per direction |
| Wizz | `be.wizzair.com/{ver}/Api/search/timetable` + auto version re-scrape from wizzair.com HTML on 404 | free | **approximate (±10%, cached)** | search + monitor; must pass exact confirmation before alerting |
| Verification | farfnd/timetable **exact-date re-query** immediately before any alert | free | exact | replaces Apify in the alert path |
| Price context | self-collected `price_history.csv` (built by every cron run) | free | — | "is this a good price" |
| [opt] Apify `makework36/flight-price-scraper` | config-gated, OFF by default | ~$1-2/yr | — | multi-airline cross-check insurance; connections data if Phase 5 wants it |
| [opt] SerpApi Google Flights | 250/mo recurring free tier | free tier | — | market-context annotation on thin-history routes only |

**Provider contract**: every result carries `source` and `price_confidence:
exact|approximate`. Every response carries `sources: {ryanair: ok|error|…,
wizz: ok|version_404_refreshed|…}`.

**Dropped**: ryanair-py (dead since 2023), Kiwi/Tequila, Amadeus Self-Service,
Skyscanner, fast-flights (v1), booking/v4/availability as primary (keep only a
@2bad-style 409→client-version fallback stub if exact-price needs arise).

**Hardening** (patterns from adambenhassen/ryanair-mcp): realistic desktop UA,
≤3 retries with backoff on 429/5xx, a global in-process rate limiter (token
bucket — not sleep-in-thread). Cross-process bursts (cron + interactive) are
accepted; the `brief` lockfile (§6) is the practical mitigation.

**Currency (HIGH-priority fix)**: farfnd is requested with `currency=EUR`, but
the Wizz timetable returns origin-market currency — **HUF for BUD**. Normalize
to EUR at the provider boundary (configurable static rate, refreshed weekly from
a free ECB source); every stats/threshold comparison is currency-checked;
`--budget`/`--max-price` are defined as EUR. One HUF row must never again
poison an average. Phase 1 exit criterion.

---

## 4. New CLI surface (intent verbs)

JSON on stdout by default (`--pretty` for humans). Every response: `results`
(semantic fields, stable `deal_id`s, `price_confidence`), `summary` (one
sentence Hermes can paste into Telegram verbatim), `sources` (per-provider
health), `next` (suggested follow-up commands). Exit codes: 0 ok (incl. empty),
1 transient (provider down — the next scheduled run is the retry), 2 input
error (agent must correct using `hint`).

```bash
# INTENT VERBS — what Hermes runs 95% of the time
flight-deals getaway --category seaside --month 2026-08 --nights 4-7 [--budget 180]
flight-deals watch add BUD-CFU --months 2026-08,2026-09 --nights 4-7 --max-price 150
flight-deals watch add --category italian-gems --month 2026-08 --budget 120
flight-deals watch list | rm <id>
flight-deals brief [--send] [--dry-run]
flight-deals check <deal_id>

# PLUMBING — kept, fixed, marked secondary in help/skill
flight-deals search … · history · stats · cache · config · destinations
```

**Call plans (explicit, so cost is knowable — full model in SEARCH-DESIGN §4):**
- `getaway` (category): **one** anywhere-mode `roundTripFares` call filtered by
  the `--where` expression + per-route Wizz timetable merge for Wizz-served
  matches → dedupe → rank → top 10. Not a 58-destination fan-out.
- `getaway` (single destination): one `roundTripFares` call with duration
  constraints + one Wizz timetable call.
- **category watch** (in `brief`): 1 anywhere-mode call per origin + Wizz
  timetables for served matches — NOT per-route calendars.
- **single-route watch**: 2 `cheapestPerDay` calls (out + in) per watched month
  + 1 Wizz timetable call. Calendars are reserved for route watches and
  open-jaw combination.
- Every `watch add` states its per-run call cost; `plan --spec` shows it for
  any spec without touching the network.

**The estimate→confirm pipeline** (core alert correctness rule): calendar data
(per-leg minima, Wizz cache) yields **estimates**, never alerts. Candidate deals
that cross a watch threshold are confirmed with an exact-date `roundTripFares`
(and/or Wizz exact) query — free — and only confirmed exact prices trigger
alerts or enter threshold comparisons.

**Alert state model** (replaces hand-waved "hysteresis"):
- `data/alert_state.json`, keyed `(watch_id, route, month)`:
  `{last_alert_price, last_alert_at, expires_at, state}`.
- States: `new → alerted → suppressed → re-armed`. Alert fires on first
  threshold crossing (confirmed price). While `suppressed`, re-alert only if a
  newly confirmed price is ≥15% below `last_alert_price` (threshold chosen to
  sit above Wizz's ±10% noise floor) or after `expires_at` (default: watched
  month ends or fare date passes). Price rising back into band does nothing.
- Testable with a fake clock: "run `brief` twice → exactly one alert".

**Deal identity & `check` semantics:**
- `deal_id` = short hash of (origin, destination, out_date, return_date,
  source) — price excluded.
- Snapshots are **append-only observations** `{deal_id, seen_at, price,
  price_confidence}` in `data/deals/`. `check <deal_id>` re-queries live exact
  price and reports delta vs the latest and the first observation. Past-dated
  deal → exit 2 with hint "dates have passed; run `flight-deals getaway …`".
- `brief` prunes past-dated snapshots and expired cache entries as a side
  effect (keeps the git-versioned data dir bounded).

**Empty states are typed** (a watched seasonal route in November must not read
as a failure): `route_status: no_service | no_match | provider_error` — three
different digest lines. `watch add` validates at creation (one calendar ping)
and warns if the route has zero flights in the watched months.

**Validation before network**: IATA fuzzy-match with suggestion in `hint`,
date sanity, window logic. `watch add` is idempotent (re-add = update).

---

## 5. Skill rewrite (the Hermes contract)

Replace the creative-usage strategy catalog with a **thin, low-freedom router**
(~60 lines), written for the weakest model that will run it:

```markdown
---
name: flight-deals
description: Finds flight deals, tracks prices, watches routes, reports on
  watched deals. Use when the user mentions flights, trips, getaways, fares,
  deals, or price alerts.
---
# Intent → command (run EXACTLY these; do not compose raw search pipelines)
| User says | Run |
|---|---|
| "find me a cheap trip / getaway to X in <month>" | flight-deals getaway --category <or --to> --month M --nights 4-7 |
| "any flight news? / how are my watches?"         | flight-deals brief |
| "watch/alert me on X under €Y"                   | flight-deals watch add ... |
| "is that CFU deal still good?"                   | flight-deals check <deal_id> |
# Rules
- Output JSON has `summary` — paste it to the user, lightly edited.
- Follow the `next` field for follow-ups. Obey `hint` on errors, retry once.
- Empty results are an answer, not a failure — report them with the hint.
# Gotchas (grows from real failures)
```

- `AGENTS.md` at repo root: the contract (never invent flags; if no intent verb
  fits, say so rather than scripting around).
- ~~`manifest` command~~ — **cut** (premature: Typer `--help` + the router table
  are the machine-readable surface; revisit only if a second consumer appears).
- **Two-agent test, measurable**: replay the `evals/` transcripts against one
  strong and one weak model; pass = identical verb + required flags on ≥90% of
  cases. Divergence → fix the skill/CLI, not the agent.
- `evals/` seeded with real failed Hermes transcripts; each failure becomes a
  Gotcha line or (better) logic absorbed into the CLI.

---

## 6. Cron / monitoring design

- **One cron entry point**: `flight-deals brief --send`, 2-3×/day via launchd
  (Personal-OS: "this is a Mac; launchd only"), residential IP, manually
  runnable, logs to a file, safe to run mid-work.
- **Concurrency is designed, not assumed**: all JSON state written atomically
  (tmp + `os.rename`); `brief` takes an `flock` — a second concurrent instance
  exits 1 with "already running"; CSVs are append-only by design.
- Paths anchored to the project root (env `FLIGHT_DEALS_HOME` → fallback to
  package-relative), so cron works from any cwd.
- **Telegram digest**: chunked at ~3500 chars (4096 API cap), HTML parse mode
  (Markdown breaks on unescaped `_` in URLs — currently a silent 400), failed
  send → exit 1 so the log surfaces it. `--dry-run` prints instead of sending.
  Deep links (Google Flights/Maps) preserved from the current formatter.
- History: `price_history.csv` is kept as-is (columns already include
  `timestamp_utc`); stats switch from departure-date windows to
  **observation-time** (`timestamp_utc`) filtering — no migration, but verified
  by a test. Fixes the "best this year" tautology badges.
- Alert dedup lives in the alert state model (§4) — hourly cron can never
  re-alert the same drop.

---

## 7. Phased roadmap

### Phase 0 — Stop the lying (deletion + honesty ONLY — small by design)
1. Delete the fake-deal blocks (orchestrator demo deal, cron_report fallback).
2. **Delete** (don't fix) the six broken commands/paths — `roundtrip`,
   `collect`, `alerts`, `history` display, `--connections` composites, dead
   Wizz parse block — each replaced by a stub error: "removed pending rebuild
   (see docs/UPGRADE-PLAN.md)". No double work on code Phase 1 rewrites.
3. Surface every swallowed exception: replace the 17 `except: pass` with logged
   errors + a `sources` line in output.
4. Anchor data paths to the project root; fix the config TTL-brick bug; remove
   `--set-telegram-token` (env-only secrets); align pyproject (version, drop
   ryanair-py).

**Exit criteria**: no command lies or crashes; every remaining command works or
says why it's gone; a failed provider is visible in output.

### Phase 0.5 — Freeze the contract (NEW; before any rebuild)
1. Write the JSON envelope schema doc: `results`/`summary`/`sources`/`next`,
   exit codes, `deal_id` format, `price_confidence`, `route_status`.
2. **Record live fixtures now, before endpoints drift**: capture real farfnd
   `roundTripFares`, `cheapestPerDay`, anywhere-mode, and Wizz timetable
   responses (incl. a 404-version case) into `tests/fixtures/`.
3. Golden JSON outputs for each intent verb (from fixtures).

**Exit criteria**: schema doc committed; fixtures committed; Phase 1-3 tests
will be written against these and never churn.

### Phase 1 — Provider core (the round-trip fix)
1. Rebuild `RyanairProvider` on farfnd: `roundTripFares` (durationFrom/To,
   anywhere-mode), `cheapestPerDay`; global rate limiter; retries; honest errors.
2. Rebuild `WizzProvider` on the timetable endpoint with version auto-discovery
   (re-scrape on 404, cache the version); results tagged `approximate`.
3. **EUR normalization at the provider boundary** (Wizz returns HUF for BUD).
4. Cache v2: keys include return windows; per-provider TTLs; atomic writes.
5. Apify: fix the cache-arg swap, gate behind config, otherwise untouched —
   **no role in the alert path**.
6. Fetch + cache the Ryanair/Wizz **route networks** (weekly TTL) so the
   planner only queries flyable routes; registry **tag audit** (category
   taxonomy per SEARCH-DESIGN §3) + seasonality + per-carrier service flags;
   ground pairs for BUD↔VIE/BTS and open-jaw city clusters.

**Exit criteria**: `BUD→CFU August, 4-7 nights` returns true paired EUR fares
from both carriers in <10s warm, per-source status shown; contract tests green
against Phase 0.5 fixtures (incl. "200-but-schema-changed → `sources:
parse_error`, not `ok`" and "Wizz 404 → version re-scrape" cases).

### Phase 2 — Spec, planner + intent verbs (spec-centric per SEARCH-DESIGN §4)
The **search spec** schema + **planner** (`plan` = compile-only with call-cost
estimate, `run` = execute with `--max-calls`) land first; `getaway`/`oneway`
are thin spec builders; `--where` tag-expression parser + `where list|show`;
`watch add/list/rm`, `brief`, `check`; JSON/`--pretty` dual output from one
formatter; corrective errors; stable deal IDs + append-only snapshots;
`alert_state.json` (`schema_version: 1`, atomic writes); estimate→confirm
pipeline; typed empty states; `search` demoted to plumbing (Layer-1 `fares`
primitives).

**Exit criteria**: the four intent verbs cover the six workflows of the old
creative-usage doc with zero flag-composition; `brief` runs cold in a fresh
shell; golden-output tests pass.

### Phase 3 — Monitoring loop
Alert state machine (fake-clock tested); observation-time history windows;
`brief --send` under launchd with flock; chunked HTML Telegram digest;
prune-on-brief. **Watches become saved searches** (`data/searches/*.yaml`:
spec + schedule + alert block per SEARCH-DESIGN §4/§6); `brief` = run due
searches → diff vs last run + history → alert state machine.

**Exit criteria**: "run `brief` twice → exactly one alert" test passes; a real
price drop alerts exactly once across a week of cron runs; digest arrives on
your phone within limits.

### Phase 4 — Agent contract
New SKILL.md router (installed to Hermes skills dir), `AGENTS.md`, `evals/`
seeded with past Hermes failures, two-agent test run (≥90% verb+flags match).
Router gains the `--where` translation table + the follow-`next`-at-most-twice
rule; `references/spec-guide.md` (one level deep) for strong agents authoring
specs; **agentic scheduled review**: `searches due --agentic` + `wake <name>`
bundles (spec + agent_prompt + last results + history context) per
SEARCH-DESIGN §6.

**Exit criteria**: weakest available model completes "find me a seaside getaway
in August under €150 and watch it" end-to-end without composing a raw search.

### Phase 5 — The shapes ladder (SEARCH-DESIGN §2; ordered by cost/risk)
- **5a — Extended origins (S3)**: run the same sweeps from VIE/BTS with
  BUD↔VIE/BTS ground cost/time added and shown. Trivial once Phase 2 exists;
  large deal unlock (Vienna's FR/W6 base).
- **5b — Open-jaw (S4)**: CAL(O→D1) × CAL(D2→O) × ground(D1↔D2) inside
  category clusters (NAP↔BRI, BCN↔VLC, ATH↔SKG…). Pure local combination on
  day-level data; no times needed.
- **5c — Via-hub self-transfer (S5, the "BUD→VIE then onward" pattern)**:
  enumerate day-level over curated hubs, then **time-verify** the shortlist
  via RT-EXACT/AVAIL with MCT ≥3h, separate-ticket risk shown, fixed risk
  buffer in the displayed total. Candidates are `unverified_connection` until
  confirmed; never alerted unverified. Start FR×FR (+FR×W6 with confirmation).
- Then the optional extras: SerpApi price-insights on thin-history routes;
  Apify cross-check as opt-in `check --deep`; Telegram deal-channel ingestion
  (Telethon) as an opportunistic signal feed.

---

## 8. Testing appendix (the recipe, not a clause)

1. **Recorded fixtures** (`tests/fixtures/`): real farfnd roundTripFares /
   cheapestPerDay / anywhere + Wizz timetable responses, captured in Phase 0.5.
2. **Golden outputs**: canonical JSON per intent verb, diffed byte-for-byte.
3. **Fake clock** (freezegun): TTLs, observation windows, alert-state expiry.
4. **Alert-state tests**: brief-twice-one-alert; re-alert only at ≥15% further
   drop; expiry re-arms.
5. **Failure honesty tests**: 200-but-changed-schema → `parse_error` not `ok`;
   Wizz 404 → version re-scrape; provider down → exit 1 + `sources` says so.
6. **Currency test**: a HUF timetable fixture must normalize to EUR and never
   enter stats raw.

## 9. What gets deleted

Fake deals · six broken commands (stubs until rebuilt) · ryanair-py + legacy
availability path · dead duplicates (`alerts`, `get_roundtrip_price`,
`get_reachable_with_connections`, unreachable Wizz block, no-op backoff
decorators, `farepy_provider.py`) · `--set-telegram-token` · the creative-usage
skill as primary contract (archived to `references/advanced.md`) ·
connections/efficiency flags until Phase 5.

## 10. Success criteria

1. **Round-trip truth**: paired-fare EUR results for ≥90% of BUD short-stay
   routes where flights exist; zero fabricated output anywhere.
2. **Agent reliability**: weak-model Hermes completes the top 4 intents via the
   router without hand-holding (measured on `evals/`, ≥90%).
3. **Monitoring**: watch → drop → exactly-one Telegram alert, demonstrated
   across a week of cron runs (fake-clock tested before real time).
4. **Trust**: any provider failure is visible in the output the agent sees;
   "no deals" always means "we looked and there were none".
5. **Cost**: default stack runs on $0; optional layers (Apify ~$1-2/yr,
   SerpApi free tier) are config-gated and never load-bearing.
