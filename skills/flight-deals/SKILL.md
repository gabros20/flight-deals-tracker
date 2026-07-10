---
name: flight-deals
description: Finds flight deals, tracks prices, watches routes, reports on
  watched deals. Use when the user mentions flights, trips, getaways, fares,
  deals, or price alerts.
---

# Intent → command (run EXACTLY these; do not compose raw search pipelines)

| User says | Run |
|---|---|
| "cheap trip/getaway to X, back in ~N days" | `flight-deals getaway --depart <window> --where "<expr>" --nights <lo-hi> [--budget N] [--from ORIGIN]` |
| "one-way to X" / no return mentioned | `flight-deals oneway --depart <window> --where "<expr>" [--budget N]` |
| "any flight news? / how are my watches?" | `flight-deals brief` |
| "watch/alert me on ROUTE under €Y" | `flight-deals watch add ORIGIN-DEST --months YYYY-MM[,YYYY-MM] --nights lo-hi --max-price Y` |
| "watch/alert me for <category> under €Y" | `flight-deals watch add --where "<expr>" --month YYYY-MM --budget Y` |
| "is that <id> deal still good?" | `flight-deals check <deal_id>` |
| "what tags/categories exist?" | `flight-deals where list` |
| "does '<word>' mean anything here?" | `flight-deals where show "<expr>"` |

`--where` is a tag expression, not free text — translate the user's words:
- "seaside or italian or spanish" → `"seaside | italy | spain"`
- "greek islands" → `"island & greece"`
- "mountains somewhere" → `"mountains | lakes"`
- "islands but not the Canaries" → `"island & !canaries"`
If you are not sure a word maps to a real tag, run `flight-deals where list`
first — never guess a tag name into `--where`.

# Rules

1. Every response is JSON with `results`, `summary`, `sources`, `next` (see
   `docs/CONTRACT.md` if you need the full shape — you shouldn't). Paste
   `summary` to the user, lightly edited; never hand-format `results` yourself.
2. Follow `next` at most TWICE, then stop and report what you have — do not
   keep widening forever.
3. On exit 2, `hint` is an exact corrected command — obey it and retry ONCE.
   On exit 1, the failure is transient (a provider is down); report it, don't
   retry in a loop.
4. Empty `results` is an answer, not a failure — relay `route_status` /
   `summary` (e.g. "no seasonal service in November") rather than treating it
   as broken.
5. Always call the JSON form (default). Never parse `--pretty` output — it is
   for humans reading a terminal, not for agents.
6. Never invent a flag or a tag. If no intent verb above fits, say so instead
   of scripting a raw pipeline.

# Worked example

> "Best deals departing Aug 22-24, seaside or italian or spanish, about a week"

```
flight-deals getaway --depart 2026-08-22..2026-08-24 --where "seaside | italy | spain" --nights 5-8
```

Read `summary` (paste-ready), skim `results` for the ones worth naming, and if
`next` offers exactly one widening move and the user still wants more, run it
— then stop (rule 2). If `results` is `[]`, relay the `route_status` reason,
don't retry blindly.

# Gotchas (grows from real failures)

- Never invent category names — run `flight-deals where list` and pick from
  there; `where show "<expr>"` sanity-checks before a big ask.
- Never call the deprecated `search` alias when `getaway`/`oneway` fits —
  they're the same engine, but `search`'s flags are legacy sugar.
- Dates in output are airport-local calendar dates, not UTC — don't adjust them.
- Don't parse `--pretty` output; use the default JSON.
- For anything beyond one intent verb (authoring a spec, exploring shapes,
  reviewing a saved search), read `references/spec-guide.md` — don't improvise.
