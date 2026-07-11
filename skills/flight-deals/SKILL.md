---
name: flight-deals
description: Finds flight deals, tracks prices, watches routes, reports on
  watched deals. Use when the user mentions flights, trips, getaways, fares,
  deals, or price alerts.
---

# Intent → command (run EXACTLY these; do not compose raw search pipelines)

| User says | Run |
|---|---|
| "cheap trip/getaway to <category>, back in ~N days" | `flight-deals getaway --depart <window> --where "<expr>" --nights <lo-hi> [--budget N] [--from ORIGIN]` |
| "how much to fly to <place> / flights to Barcelona" | `flight-deals getaway --to <IATA\|city> --depart <window> --nights <lo-hi>` (one-way: `flight-deals oneway --to <IATA\|city> --depart <window>`) |
| "one-way / single leg to X" | `flight-deals oneway --depart <window> --where "<expr>" [--budget N]` |
| "any flight news? / how are my watches doing?" | `flight-deals brief` |
| "what am I watching? / list my watches" | `flight-deals watch list` |
| "stop/cancel the X watch" | `flight-deals watch rm <name>` |
| "watch/alert me on ROUTE under €Y" | `flight-deals watch add ORIGIN-DEST --months YYYY-MM[,YYYY-MM] --nights lo-hi --max-price Y` |
| "watch/alert me for <category> under €Y" | `flight-deals watch add --where "<expr>" --month YYYY-MM [--nights lo-hi] [--budget Y]` |
| "is that <id> deal still good?" | `flight-deals check <deal_id>` |
| "what tags/categories exist?" | `flight-deals where list` |
| "does '<word>' mean anything here?" | `flight-deals where show "<expr>"` |

`brief` answers "any news on what I track"; `watch list` is the config (what's
tracked, not today's prices).

# Choosing the verb

- A **named place** (Barcelona, Milan, BCN) is `--to`; a **category** (seaside,
  islands, italy) is `--where`. Never both — they are mutually exclusive.
- Return/duration unstated on a leisure ask → default to `getaway --nights 3-7`
  (state the default to the user), or ask. Use `oneway` only when the user
  explicitly says one-way / single leg.
- `--where` is a tag expression, not free text — translate the user's words:
  "seaside or italian or spanish" → `"seaside | italy | spain"`; "greek islands"
  → `"island & greece"`; "islands but not the Canaries" → `"island & !canaries"`.
  Unsure a word is a real tag? Run `flight-deals where list` first — never guess.
- A watch with no `--nights` is a one-way watch: getaway watches need `--nights`.
- Cheaper trip shapes are opt-in: add `--shapes direct,extended-origin,open-jaw`
  to `getaway` to also consider nearby-airport departures (VIE/BTS, ground cost
  shown) and open-jaw city pairs (fly into one city, home from another). Default
  is `direct` only; a non-direct shape appears only when it genuinely beats direct.

# Rules

1. Every response is JSON with `results`, `summary`, `sources`, `next` (see
   `docs/CONTRACT.md` for the full shape). Paste `summary` to the user, lightly
   edited; never hand-format `results` yourself.
2. Follow `next` at most TWICE, then stop and report — don't widen forever.
3. On exit 2, `hint` is an exact corrected command — obey it and retry ONCE.
   On exit 1 the failure is transient (a provider is down); report it, don't
   retry in a loop.
4. Empty `results` is an answer, not a failure — relay `route_status` / `summary`
   (e.g. "no seasonal service in November"), don't treat it as broken.
5. Always call the default JSON form; never parse `--pretty` (that's for humans).
6. Never invent a flag or a tag. If no intent verb above fits, say so instead of
   scripting a raw pipeline.

# Worked example

> "Best deals departing Aug 22-24, seaside or italian or spanish, about a week"

```
flight-deals getaway --depart 2026-08-22..2026-08-24 --where "seaside | italy | spain" --nights 5-8
```

Read `summary` (paste-ready), skim `results` for the ones worth naming, and if
`next` offers one widening move and the user wants more, run it — then stop
(rule 2).

# Gotchas (grows from real failures)

- A city is not a tag: use `--to` for named places, `--where` for categories.
- Holidays aren't dates: translate "christmas" to a `--depart 2026-12-19..2026-12-27`
  style window yourself; the CLI only takes dates.
- Never invent category names — run `flight-deals where list`; `where show
  "<expr>"` sanity-checks before a big ask.
- **Broad `--where` + many airports:** `getaway` may exit 2 with `PlannerRefusal` and `hint` to re-run with `--max-calls 50` (or narrow the expression). Obey the hint once.
- **Aliases in `where list`:** phrases like `european-islands` and `italian-gems` map to `island` and `italy` — use them in expressions; still run `where show` before a big cron sweep.
- Never call the deprecated `search` alias when `getaway`/`oneway` fits (including cron round-trip watches — `getaway` pairs outbound/return).
- When `sources.wizzair` is `error` but `ryanair` is `ok`, report results anyway and note Wizz gaps; flag `approximate` prices as estimates.
- Dates in output are airport-local calendar dates, not UTC — don't adjust them.
- For anything beyond one intent verb (authoring a spec, exploring shapes,
  reviewing a saved search), read `references/spec-guide.md` — don't improvise.
