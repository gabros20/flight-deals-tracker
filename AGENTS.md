# AGENTS.md — the contract for any agent working in this repo

This file is the repo-root contract (UPGRADE-PLAN §5). If you are Hermes or
Claude Code answering a user's flight/travel request, start at
`skills/flight-deals/SKILL.md` (the router) — this file governs how you
operate on the tool and this codebase in general.

## The contract

1. **JSON is the interface.** Every `flight-deals` command prints one JSON
   object on stdout: `results`, `summary`, `sources`, `next`, plus
   `error`/`hint` on failure (`docs/CONTRACT.md`). `--pretty` renders the same
   fields for a human terminal — it is not a second data path, and you must
   never parse it. Read the default JSON output, always.
2. **Never compose a raw pipeline when a verb fits.** `getaway`, `oneway`,
   `check`, `watch add`, `brief`, `searches …` cover the vast majority of
   requests (see `skills/flight-deals/SKILL.md`'s intent table). Reaching for
   `fares`/`routes`/other primitives, or chaining shell commands, when an
   intent verb already does the job is exactly the failure mode this refactor
   removed — don't reintroduce it.
3. **Never invent a flag, a tag, or a category.** If a `--where` tag might not
   exist, run `flight-deals where list` / `where show "<expr>"` first — don't
   guess. If no documented flag does what you want, say so; don't paste an
   undocumented flag and hope.
4. **If no intent verb fits, say so.** Report to the user that the request is
   outside the current CLI surface rather than fabricating a workaround
   (scripting Python against internals, hand-building a spec you can't
   validate, etc.). Strong agents may drop to the spec layer
   (`skills/flight-deals/references/spec-guide.md`) deliberately — that's
   different from silently working around a gap.
5. **State files are the tool's, not yours.** `data/searches/*.yaml`,
   `data/alert_state.json`, `data/searches/.runs.json`,
   `data/searches/.results/*.json`, `data/price_history.csv`, `data/deals/*`
   are written atomically by the CLI and carry `schema_version`. Never
   hand-edit them — use `flight-deals searches add/rm`, `watch add/rm`, etc.
   A hand-edit that skips validation is exactly how a saved search silently
   breaks `brief`.
6. **Empty results are a valid, successful answer.** Exit 0 with
   `results: []` and a `route_status` is not a failure to work around —
   relay it honestly (`summary` already explains why).
7. **Money is EUR; dates are airport-local.** Don't reinterpret `price_eur` or
   convert it; don't shift a date for a timezone that doesn't apply to a
   calendar date.

## For code changes to this repo

- Read `docs/UPGRADE-PLAN.md` and `docs/SEARCH-DESIGN.md` before touching the
  engine/planner/registry — they are the authoritative design, not this file.
  `docs/CONTRACT.md` freezes the output schema; don't invent fields.
- Follow the Global Constraints in `.orchestrate/plan.md` if you're doing
  refactor work on this branch (deps, atomics, EUR-canonical money, no
  fabricated data, tests never hit live endpoints).
- Small, coherent commits; run the full test suite before calling anything
  done.

- **Presenting results**: always use the standard format in skills/flight-deals/SKILL.md §"Presenting results" — summary line, then ≤5 two-line deal cards built only from envelope fields, `~`/confidence markers verbatim, no tables.
