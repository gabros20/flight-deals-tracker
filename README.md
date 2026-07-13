# Flight Deals Tracker

A CLI for finding and monitoring Ryanair & Wizz Air deals — free public
endpoints only, JSON on stdout, built to be driven by an agent (Hermes/Claude
Code) as much as by hand.

## What it is now

This is the finished, post-rebuild surface (18 gated tasks, 560 tests green).
See `docs/UPGRADE-PLAN.md` (historical planning record — now marked
COMPLETED) and `docs/SEARCH-DESIGN.md` (as-built search model: trip shapes,
`--where` algebra, gems) for the design; `docs/CONTRACT.md` for the frozen
output schema. If you're an agent, start at `skills/flight-deals/SKILL.md`,
not this file.

## Install

```bash
pip install -e .
```

Python 3.11+. No API keys required for the core flow (Ryanair `farfnd` and
Wizz Air's public timetable are both free, unauthenticated endpoints). An
optional `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` pair (env vars only, never
written to a config file) enables `brief --send` digests.

## The CLI surface

**Intent verbs** — what you (or an agent) run 95% of the time:

```bash
flight-deals getaway --depart 2026-08-22..2026-08-24 --where "seaside | italy | spain" --nights 5-8 [--budget 180]
flight-deals oneway  --depart 2026-08-22..2026-08-24 --where "seaside | italy"          [--budget 120]
flight-deals getaway --to Barcelona --depart 2026-08-22..2026-08-24 --nights 5-8   # named place, not a tag
flight-deals getaway --to Halki --depart 2026-06-01..2026-06-07 --nights 4-7       # a "hidden gem" island (89 in the catalog)
flight-deals getaway --depart 2026-08-22..2026-08-24 --where "seaside" --nights 5-8 --shapes direct,extended-origin,open-jaw,via-hub
flight-deals check <deal_id>
```

`--shapes` opts into cheaper, more creative trip shapes beyond a direct
round-trip: `extended-origin` (nearby-airport sweeps), `open-jaw` (fly in one
city, home from another), and `via-hub` (S5 — a time-verified self-transfer:
two separate tickets through a hub, only ever surfaced once both legs are
verified to actually connect, with the risk and a displayed ~€25 buffer
stated honestly). `--to` targets a single named place — an IATA code, a
city, or a gem slug (`--where` targets a tag expression instead; the two are
mutually exclusive).

**Monitoring** — saved searches the deterministic `brief` loop runs on a
schedule:

```bash
flight-deals watch add BUD-CFU --months 2026-08,2026-09 --nights 4-7 --max-price 150
flight-deals watch add --where "seaside & italy" --month 2026-08 --budget 120
flight-deals watch list | rm <name>
flight-deals searches list | add | rm | show | due [--agentic]
flight-deals brief [--send] [--dry-run] [--all]
flight-deals wake <name>          # bundle a saved search for an agentic review session
```

**Spec layer** — for authoring/inspecting a search declaratively (strong
agents; see `skills/flight-deals/references/spec-guide.md`):

```bash
flight-deals plan --spec <file|json|->     # compile only — no network
flight-deals run  --spec <file|json|->  [--max-calls 40]
flight-deals where list | show "<expr>"
```

`search` still exists as a deprecated true alias of `oneway`, kept for
backward compatibility only.

## The JSON contract

Every command prints exactly one JSON object on stdout: `results`, `summary`
(paste-ready, one sentence), `sources` (per-provider status), `next` (at most
one follow-up command), plus `error`/`hint` on failure. `--pretty` renders the
same fields for a human terminal — never a second data path, never something
to parse. Exit codes: `0` ok (including empty results — a typed
`route_status` is a successful answer, not a failure), `1` transient/provider
failure, `2` input error (the `hint` is an exact corrected command). Full
schema: `docs/CONTRACT.md`.

## What's honest

Every price and duration is labeled by how sure the tool is, never presented
as more certain than it is. `price_confidence` is `exact` (Ryanair `farfnd`)
or `approximate` (Wizz Air) — a shaped deal reports the *weakest* confidence
among its legs. Any `why` clause built from a modeled or scheduled-but-unfared
figure (ground transfers, S5 self-transfer buffers) carries a `~` prefix so a
human or agent can tell "priced" from "estimated" at a glance. Ground data is
tiered curated > scheduled (Transitous) > scheduled-hybrid > computed (OSRM
model), and the tier is never hidden. An S5 self-transfer is never displayed
or alerted until both legs are time-verified to actually connect — an
unverified candidate simply doesn't exist as far as the output is concerned.
`route_status` values report genuine absence (e.g. no service) as a
successful, typed answer rather than an error. See `docs/CONTRACT.md` §2 for
the full field-by-field rules.

## Further reading

- `docs/CONTRACT.md` — the frozen output schema (every field, confidence and
  status enum, exit codes).
- `docs/OPERATIONS.md` — running `brief` on a schedule (launchd), state
  files, troubleshooting.
- `docs/SEARCH-DESIGN.md` — the search model: primitives, trip shapes
  (S1-S5), the `--where` tag algebra, gems.
- `docs/research/GEM-CATALOG.md` — the 89-island "hidden gem" catalog and
  its curation rules (KEEP/MARGINAL/DROP).
- `docs/explainer.html` — a narrated walkthrough of the system for humans.

## The free stack

- **Ryanair** via the public `farfnd` endpoint — exact prices, anywhere/exact/
  calendar modes.
- **Wizz Air** via its public timetable endpoint — approximate prices (never
  used to trigger an alert directly; the estimate→confirm pipeline re-queries
  an exact date before anything crosses a threshold).
- **OSRM** (public, out-of-band) precomputes a ground matrix
  (`scripts/refresh_ground.py` → `data/ground_matrix.json`) so open-jaw trips
  work for any nearby registry pair — never called from the request path.
- No paid API is required for search, watches, or the monitoring brief. Money
  is EUR-canonical everywhere; non-EUR provider responses are converted at the
  provider boundary.

## Monitoring in production

`flight-deals brief --send`, run 2-3×/day via `launchd` (macOS; no cron), is
the one entry point for the reliability backbone: due saved searches run,
confirmed prices are diffed against an alert state machine (exactly-once,
15%-drop re-alert while suppressed), and a Telegram digest goes out only when
there's news. Full setup, state-file map, and troubleshooting:
`docs/OPERATIONS.md`.

## Agent integration

- `skills/flight-deals/SKILL.md` — the low-freedom router any agent should
  read first (intent table, `--where` translation, rules, a worked example).
- `skills/flight-deals/references/spec-guide.md` — the spec schema, worked
  examples, and failure modes for a strong agent working at the spec layer.
- `skills/flight-deals/references/advanced.md` — the old creative-usage
  strategy catalog, archived (superseded by the router).
- `AGENTS.md` (repo root) — the operating contract: JSON is the interface,
  never invent a flag, state files are the tool's.
- `evals/` — seed utterance → expected-command cases for replaying the skill
  against models of different strength.

## License

MIT
