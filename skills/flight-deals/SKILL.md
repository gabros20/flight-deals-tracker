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
| "cheap trip to a small island / hidden gem" | `flight-deals getaway --to <gem> --depart <window> --nights <lo-hi>` (or by category: `--where "hidden-gem & greece"`) |
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
  → `"island & greece"`; "islands but not the Canaries" → `"island & !canaries"`;
  "the Azores" → `"azores"` (Ponta Delgada/Terceira — note: no Ryanair service, so
  a via-hub self-transfer there won't surface; the tag still lists the airports).
  Unsure a word is a real tag? Run `flight-deals where list` first — never guess.
- A watch with no `--nights` is a one-way watch: getaway watches need `--nights`.
- **Gems** (small islands reached by gateway airport + ferry/bus) are places,
  not airports: `--to Halki` (a slug/name), not an IATA. `--to <gem>` shows only
  the gem-extended options (fly to the gateway, then the onward chain, cost ×2
  for a round-trip); `--where` matching a gem's tags shows both the plain gateway
  deal AND the gem variant. The extended total (fare + onward) is what budget and
  watches compare. `where show "<expr>"` lists which gems a category reaches.
- Cheaper trip shapes are opt-in: add `--shapes direct,extended-origin,open-jaw,via-hub`
  to `getaway` to also consider nearby-airport departures (VIE/BTS, ground cost
  shown), open-jaw city pairs (fly into one city, home from another), and via-hub
  self-transfers (two separate tickets through a hub). Default is `direct` only;
  a non-direct shape appears only when it genuinely beats direct. `via-hub` needs
  a `--nights` range (it's a round-trip through a hub).

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

## Presenting results (the standard format — use it EVERY time)

Line 1: the envelope `summary`, lightly edited; append the coverage caveat when `sources` shows a failure.
Then up to 5 deals, each in exactly this two-line shape (omit a part only when the field is absent):

```
1. ✈️ BUD→CFU · Aug 22–27 (5n) · €113 [exact]
   Ryanair 06:25 · 29% below typical · id 24affe56a9
2. ✈️ BUD→NAP→Halki ⛴️ (gem) · Sep 3–9 (6n) · €207 [exact + ~€20 onward]
   then bus 30m + ⛴️ 1h15 · seasonal ferry · id 8c1d02aa41
```

- Non-direct suffixes: `via VIE 🚌` (extended origin) · `open-jaw ⛴️/🚆` · `self-transfer ⚠ separate tickets` (S5) · `→ <gem> ⛴️ (gem)`.
- Copy `~` markers and `[exact|approximate]` verbatim from the JSON — never add or remove precision; totals come from `price_eur` only.
- Include the tool's booking/maps links as plain URLs on the detail line when the channel renders them.
- Empty results: relay `summary`, then offer the single `next` suggestion as a question ("Widen budget to €190?").
- `brief`/alert digests: relay the digest text as-is, prefixed 🔔.
- Never tables. Never invented fields. More than 5 results: say how many more exist and offer to show them.

# Gotchas (grows from real failures)

- A city is not a tag: use `--to` for named places, `--where` for categories.
- Gems are places, not airports: `--to Halki` works (a gem slug/name), `--to RHO`
  gets you only the gateway. Marginal/day-trip gems (awkward connections) are
  hidden from `--where` matching and reachable ONLY via explicit `--to`; their
  variant carries the caveat in its `why`. Out-of-season gems drop out of
  `--where` automatically for a window outside their season.
- **Self-transfer = separate tickets, the risk is the traveller's.** A via-hub
  (`shape: "S5"`) deal is TWO separate bookings through a hub — if the first
  flight is late you miss the second and no one refunds you. The tool enforces a
  3h minimum connection and only ever shows time-VERIFIED self-transfers, but you
  MUST relay the `connection.separate_tickets` risk (it's in the `why` and
  `summary` already) — never present an S5 as if it were one protected ticket.
  The price already includes the displayed ~€25 self-transfer buffer.
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
