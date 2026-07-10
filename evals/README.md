# evals/ — replaying the skill against real utterances

This is the "two-agent test" from `docs/UPGRADE-PLAN.md` §5: replay each case
in `cases/` against a model that has ONLY read `skills/flight-deals/SKILL.md`
(the weak-model contract) and separately against one that has also read
`skills/flight-deals/references/spec-guide.md` and `AGENTS.md` (the strong-
model contract). Pass = the model's chosen command matches `expected_command`
on verb + every required flag (values may legitimately differ where the case
says so — e.g. a relative date resolved against "today").

This directory does not (yet) run agents automatically — that's future work,
not required by this task. Replay is a short manual/scripted loop:

1. Give the model under test **only** the router (`SKILL.md`) — or, for a
   strong-agent case, the router + `references/spec-guide.md` + `AGENTS.md` —
   plus the case's `utterance`. Nothing else about this task.
2. Capture the exact command line it produces (or its clarifying question,
   for the ambiguous case).
3. Compare against `expected_command` (or `expected_behavior` for the
   ambiguous case): same verb, same required flags, values resolved
   consistently. Log the actual vs. expected in your own scratch notes —
   there's no scoring harness checked in here.
4. **Divergence → fix the skill or the CLI, not the agent** (UPGRADE-PLAN
   §5). A repeated failure mode becomes a new line in `SKILL.md`'s Gotchas
   section, or — better — logic absorbed into the CLI itself (a clearer
   `hint`, a `next` suggestion, an alias) so future models don't need the
   Gotcha at all.

## Case file format

Each `cases/NN-slug.yaml`:

```yaml
id: 01-basic-getaway
utterance: "the user's exact request, verbatim"
expected_command: "flight-deals getaway --depart 2026-08-22..2026-08-24 --where \"seaside | italy | spain\" --nights 5-8"
notes: "why this is the right translation; any acceptable variation"
source: "where this utterance came from (worked example, old skill, made up for coverage)"
```

The one ambiguous case (`06-ambiguous-no-category.yaml`) replaces
`expected_command` with `expected_behavior`: the model must ask a clarifying
question or run `flight-deals where list` — it must NOT invent a tag or a
date window from nothing.

## Seed cases

| # | Utterance (short) | Expected verb | Why it's here |
|---|---|---|---|
| 01 | "Best deals Aug 22-24, seaside/italian/spanish, about a week" | `getaway` | The worked example SKILL.md leads with (SEARCH-DESIGN §5) |
| 02 | "Is that CFU deal still good?" | `check` | Deal-identity follow-up, not a fresh search |
| 03 | "Cheap **one-way** European island flights from Budapest in August under €120" | `oneway` | Explicit "one-way" is the discriminative signal — `oneway`, not the `getaway --nights 3-7` default the router uses when duration is unstated |
| 04 | "Track BUD to CFU and alert me if it drops" | `watch add` | Adapted from the old creative-usage doc's "history-driven hunting" / `track` workflow — the removed percentage-threshold `track` command is now `watch add` + `brief` |
| 05 | "Watch seaside deals for August, alert under €150" | `watch add` | Category watch, the second common monitoring shape |
| 06 | "Find me something nice for a getaway" | *(ambiguous)* | No category, no dates — must ask or run `where list`, never invent |

Case 04 is adapted from the old creative-usage doc's workflow (the removed
`track --threshold` command); that doc is archived at
`skills/flight-deals/references/advanced.md`. Case 03 pins the explicit
one-way vs. default-getaway routing distinction.
