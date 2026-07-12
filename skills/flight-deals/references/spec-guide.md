# Spec guide (for strong agents)

This is the one-level-deep reference `SKILL.md` points to. It assumes you've
read the router and are past intent verbs: you want to author a spec, sanity
check its cost, explore primitives, or review/mutate a saved search. Full
design rationale lives in `docs/SEARCH-DESIGN.md` §3-§6; this doc is the quick
working reference, not a replacement for reading it once.

## Spec schema

```yaml
schema_version: 1               # saved searches only; a bare spec omits this
name: august-seaside            # saved searches only
spec:
  origins: [BUD]                 # list of 3-letter IATA; default [BUD]
  where: "seaside & (italy | spain | greece)"   # tag expression; omit for a single-route watch
  destinations: [CFU]            # optional: pin to specific IATA(s) (a route watch)
  gem: halki                      # optional: set by --to <gem> alongside destinations; persists
                                  # the gem so a saved watch replays the onward extension on brief
  depart: 2026-08-22..2026-08-24 # date | window A..B | month YYYY-MM | comma list
  nights: 5-8                    # "lo-hi"; omit entirely for one-way (S1)
  shapes: [direct]                # direct (S2)|extended-origin (S3)|open-jaw (S4)|via-hub (S5)
                                  # getaway: --shapes direct,extended-origin,open-jaw,via-hub (default direct)
  via: auto                       # via-hub hub selection: auto | [VIE, BGY] | none (ignored by other shapes)
  budget: 180                     # EUR, total per person; omit for no cap
  carriers: [ryanair, wizzair]     # default both
  max_results: 10
schedule: "daily 08:30"          # optional; saved searches only -> brief picks it up
alert:
  max_price: 150                 # optional; presence makes this a "watch"
  notify: telegram
agent_prompt: |                  # optional; saved searches only -> agentic review (see below)
  Weekly: look at the results, try one variation, message only if ≥25% below typical.
```

`--where` grammar: `&` (and), `|` (or), `!` (not), parens, bare tag = itself.
Run `flight-deals where list` for the live tag inventory (with counts) and
`flight-deals where show "<expr>"` to preview matches before spending calls.

## The three CLI layers

- **Intents** (`getaway`/`oneway`/`check`) — thin builders over the spec;
  what `SKILL.md` documents.
- **Spec** (`plan`/`run`/`searches …`/`wake`) — this layer: author a spec by
  hand or from flags, inspect its cost, save it, wake a saved one.
- **Primitives** (`fares rt|calendar|timetable`, `routes`, `where`) — raw
  provider calls for exploration; the planner uses these under the hood.

## Worked examples

**1. Compile-only cost check before running anything:**
```bash
flight-deals plan --spec '{"origins":["BUD"],"where":"seaside","depart":"2026-08","nights":"5-8"}'
# -> {"calls":[...], "estimated_calls": N, "estimated_seconds": S} — no network
```

**2. Run a one-off spec (bypassing the intent flags), capped:**
```bash
flight-deals run --spec my-spec.yaml --max-calls 40
```

**3. Save a category watch (a "watch" = a saved search with an `alert` block):**
```bash
flight-deals watch add --where "seaside & italy" --month 2026-08 --nights 5-8 --budget 120
# equivalent to:
flight-deals searches add --spec '{"origins":["BUD"],"where":"seaside & italy","depart":"2026-08","nights":"5-8","budget":120}' \
  --name seaside-italy --schedule "daily 08:30" --max-price 120
```

**4. Save a single-route watch (pins the planner via `destinations`):**
```bash
flight-deals watch add BUD-CFU --months 2026-08,2026-09 --nights 4-7 --max-price 150
```

**4b. Gem destinations (island reached via gateway airport + onward ferry/bus):**
```bash
# By name/slug — ONLY gem-extended options (fly to the gateway, then the onward
# chain); the extended total (fare + onward, ×2 for a round-trip) is what --budget
# and any watch compares. Marginal/day-trip gems are reachable ONLY this way.
flight-deals getaway --to Halki --depart 2026-06-01..2026-06-07 --nights 4-7
# By category — a --where that matches a gem's tags shows BOTH the plain gateway
# deal AND the gem variant; out-of-season gems drop out for a window outside season.
flight-deals getaway --where "hidden-gem & greece" --depart 2026-06-01..2026-06-07 --nights 4-7
# See which gems a category reaches (marginal ones flagged) before a big sweep:
flight-deals where show "hidden-gem & greece"   # -> {airports:[...], gems:[{name, gateways, marginal, season}]}
```
The Deal for a gem carries additive `onward` `{name, legs, cost_eur, minutes,
note, has_ferry}` and `destination_display` ("Halki (via RHO)"); its `deal_id`
gains a `|gem:<slug>` component so it never collides with the plain gateway deal.
Gems are a terminal extension, not a shape — `--shapes` is unrelated. S4 open-jaw
deals are not gem-extended. `watch add --to <gem> ...` persists the gem on the
saved spec's `gem` field, so a scheduled `brief` run replays the gem-only
extension (and alerts on the extended total) exactly like the interactive command.

**5. A saved search with an `agent_prompt`, then wake it for review:**
```bash
flight-deals searches add --spec august-seaside.yaml --name august-seaside \
  --schedule "weekly mon 08:30" \
  --agent-prompt "Try one variation (shift window ±3 days, or swap greece for croatia); message only if ≥25% below typical."
flight-deals wake august-seaside
# -> {spec, agent_prompt, last_result, history, allowed_moves, ...} — see below
```

## `wake` and the agentic review loop

`flight-deals searches due --agentic` lists due saved searches that carry an
`agent_prompt` — the periodic, cheap-to-skip periphery beside the
deterministic `brief` loop (SEARCH-DESIGN §6). `flight-deals wake <name>`
bundles everything a review session needs, read-only, no network:

- `spec` / `agent_prompt` / `schedule` / `alert` as saved;
- `last_result` — the envelope from the most recent `brief`/`run` of this
  search (`null` if it has never run — not an error);
- `history` — `history.compare` for each route the last run actually
  returned (median, min, `pct_vs_typical`, `sufficient`);
- `allowed_moves` — the FIXED list of sandboxed mutations you may try:
  `shift_window`, `widen_nights`, `swap_where_tag`, `adjust_budget`,
  `message_decision`, `persist_variation`. Nothing outside this list —
  never hand-edit a saved search's YAML; persist a variation with
  `flight-deals searches add --name <name> --spec <file|->` (idempotent).

Sanity-check any variation with `flight-deals plan --spec ...` before running
or saving it.

To run this loop on a schedule (the `searches due --agentic` → `wake` → agent
driver, with a ready-to-cron `deploy/agentic-wake.sh`), see
`docs/OPERATIONS.md` §4a "Agentic review".

## Failure modes

- **Shapes.** `direct` (S2), `extended-origin` (S3, adds nearby-airport
  round-trip sweeps such as VIE/BTS with ground cost folded into `price_eur`),
  `open-jaw` (S4, fly into D1 / ground to D2 / fly home from D2), and `via-hub`
  (S5 self-transfer, two separate tickets through a hub) are all enabled. On
  `getaway`: `--shapes direct,extended-origin,open-jaw,via-hub` (default
  `direct`). A non-direct shape only surfaces when it genuinely beats direct
  (S1/S2/S3/S5 to the same destination dedupe cheapest-wins); S4 is a separate
  two-city deal. Open-jaw (S4) deals may include a FERRY crossing —
  `ground.has_ferry: true` marks them and the `why` leads the hop with ⛴ (e.g.
  "fly into HER, ⛴ ~4h ~€45 ferry, fly home from JTR"); relay the crossing to
  the user, it's not a train.
- **Via-hub (S5) is a self-transfer.** It needs a `nights` range (a round-trip
  through a hub) — requesting it one-way exits 2 with a hint to add `nights`.
  Only time-VERIFIED self-transfers (≥3h same-airport connection, both
  directions) ever surface; the price includes a displayed ~€25 self-transfer
  buffer, and the deal carries a `connection` object + a `separate_tickets`
  disclosure in its `why`/`summary`. ALWAYS relay that risk — a missed
  connection is the traveller's own (two separate bookings, no protected
  connection). `via` picks the hubs: `auto` (default), an explicit `[VIE, BGY]`,
  or `none`. `check` on an S5 declines (re-run the getaway to re-verify).
- **`--max-calls` exceeded.** `run`/`brief` refuse a plan whose `estimated_calls`
  exceeds the cap: `plan needs 57 calls, over the --max-calls 40 cap`. The
  hint gives the exact fix — narrow (tighter `--where`, fewer origins) or
  raise `--max-calls` to the stated number. Narrowing is almost always the
  right move; raising the cap should be rare and deliberate.
- **Unknown/misspelled tag in `--where`.** `where show`/`plan`/`run` return
  `unknown_tags` + a did-you-mean `hint` rather than silently matching
  nothing — obey it, don't guess a second tag name.
- **A departure window entirely in the past.** Rejected before any network
  call, with a hint suggesting a nearby future window.
