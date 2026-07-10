# Flight Deals Tracker

A CLI for finding and monitoring Ryanair & Wizz Air deals — free public
endpoints only, JSON on stdout, built to be driven by an agent (Hermes/Claude
Code) as much as by hand.

## What it is now

This is the post-rebuild (v0.7+) surface. See `docs/UPGRADE-PLAN.md` and
`docs/SEARCH-DESIGN.md` for the design; `docs/CONTRACT.md` for the frozen
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
flight-deals check <deal_id>
```

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

## The free stack

- **Ryanair** via the public `farfnd` endpoint — exact prices, anywhere/exact/
  calendar modes.
- **Wizz Air** via its public timetable endpoint — approximate prices (never
  used to trigger an alert directly; the estimate→confirm pipeline re-queries
  an exact date before anything crosses a threshold).
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
