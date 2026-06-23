# Flight Deals Tracker - Creative Usage Skill

**Skill for Hermes Agent**: How to use `flight-deals` CLI creatively, flexibly, and without getting stuck. Focus on flag combinations, search strategies, and adaptive workflows for finding the best Ryanair & Wizz Air deals from BUD (or other origins).

## Core Philosophy
- Always start broad, then narrow.
- Use **date windows** (`--date-from` / `--date-to` and return windows) instead of single dates.
- Combine **direct** (fast local farfnd) + **connections** (Apify + ground transport).
- Use **history** and **collect** to find price drops vs historical averages.
- Never get stuck: if no results, change one variable at a time (dates, connections, category, --fresh).
- Output is **always** numbered list with 📍 Maps, 🏞️ Images, ✈️ Google Flights links (enforced).

## Main Commands & Key Flags

### 1. `search` — The most powerful command
```bash
flight-deals search \
  --category <tag> \
  --from BUD \
  --date-from 2026-07-05 --date-to 2026-07-12 \
  --return-from 2026-07-10 --return-to 2026-07-19 \
  --max-price 180 \
  --connections \
  --max-ground-minutes 240 \
  --ground-prefer any \
  --sort-by price \
  --history-window 30 \
  --fresh
```

**Critical flags for creativity**:
- `--category`: european-islands, seaside, italian-gems, shopping (see `destinations --tag`)
- `--connections` / `--with-stops`: Unlocks 1-stop + self-transfer options via multi-airport hubs.
- `--return-from` + `--return-to`: Enables round-trip mode (recommended for getaways).
- `--fresh`: Bypass cache when you suspect stale data.
- `--history-window`: Compare against recent prices.
- `--max-ground-minutes` + `--ground-prefer`: Tune realistic connections (important for Milan, Istanbul, London airports).
- `--sort-by`: price | total-time | efficiency

### 2. `roundtrip` — Targeted for one destination
```bash
flight-deals roundtrip \
  --origin BUD --destination CTA \
  --outbound-from 2026-07-08 --outbound-to 2026-07-10 \
  --return-from 2026-07-12 --return-to 2026-07-15 \
  --max-price 200
```

### 3. Supporting commands
- `collect --category seaside --date-from ... --date-to ... --connections`: Log current prices to history (run before searching for drop detection).
- `history-stats --origin BUD --destination CTA --window 30`: See average prices and trends.
- `cache clear` or `cache stats`: Manage cache.
- `destinations --tag european-islands`: Explore available airports.
- `multi_airports`: See supported self-transfer hubs (BGY/MXP, IST/SAW, etc.).
- `config --set-default-origin BUD`

## Creative Strategies & Flag Combinations (To Never Get Stuck)

### Strategy 1: "Broad → Narrow" (Most Effective)
1. Start with wide date windows + a category.
2. Run with `--connections` and without.
3. Then tighten dates or add `--max-price`.
4. Use `--fresh` if results look old.

**Example creative sequence**:
```bash
# Phase 1: Broad exploration
flight-deals search -c european-islands --date-from 2026-07-01 --date-to 2026-07-20 --return-from 2026-07-05 --return-to 2026-07-25 --connections --fresh

# Phase 2: Filter promising ones
flight-deals search -c european-islands --date-from 2026-07-08 --date-to 2026-07-10 --return-from 2026-07-14 --return-to 2026-07-18 --max-price 150

# Phase 3: Deep dive on one island
flight-deals roundtrip -d CFU --outbound-from 2026-07-08 --outbound-to 2026-07-10 --return-from 2026-07-15 --return-to 2026-07-18
```

### Strategy 2: Direct vs Connections Toggle
- **Direct first** (no `--connections`): Faster, usually cheaper Ryanair/Wizz.
- **Then with `--connections`**: Use when direct is expensive or no dates work.
  - Tune `--max-ground-minutes 180-300`
  - Try `--ground-prefer driving` or `public`
- Apify (when configured) adds Google Flights / Kiwi connections automatically when `--connections` is used.

### Strategy 3: Date Window Creativity
- Use overlapping flexible windows instead of exact dates.
- For 4-7 night getaways: Set outbound window 3-4 days wide + return window 4-6 days later.
- Example for "any weekend in July":
  ```bash
  --date-from 2026-07-03 --date-to 2026-07-05 \
  --return-from 2026-07-10 --return-to 2026-07-12
  ```

### Strategy 4: History-Driven Hunting (Avoid Overpaying)
```bash
# 1. Collect baseline
flight-deals collect -c seaside --date-from 2026-07-01 --date-to 2026-07-31 --connections

# 2. Check stats
flight-deals history-stats --destination CFU --window 60

# 3. Search with history comparison
flight-deals search -c seaside ... --history-window 45
```

Look for prices below historical avg (alerts are logged in `collect`).

### Strategy 5: Category Chaining for Inspiration
```bash
for cat in european-islands seaside italian-gems shopping; do
  flight-deals search -c $cat --date-from ... --return-from ... --connections --max-price 160 | head -10
done
```

### Strategy 6: When You Get Zero Results (Anti-Stuck Protocol)
1. Widen date windows by 3-5 days.
2. Add `--connections`.
3. Remove `--max-price`.
4. Try `--fresh`.
5. Switch category (e.g. seaside → italian-gems).
6. Check `destinations --tag <category>` to confirm airports are reachable.
7. Use `multi_airports` to understand self-transfer options.
8. Run `collect` first, then search.

### Strategy 7: Apify + Local Hybrid (Advanced)
- Local farfnd = fast Ryanair direct roundtrips.
- Apify (when token configured) = best for connections and other airlines.
- Command pattern:
  ```bash
  flight-deals search -c seaside ... --connections   # uses both
  ```

### Strategy 8: Cron / Tracking Workflows
- Use `collect` in cron jobs for categories you care about.
- Set up price drop alerts via `track` or history detection.
- Example cron idea: Daily broad search for european-islands with --connections, log to history.

## Recommended Workflows for Common Goals

**Goal: Cheap 5-7 night seaside getaway in July from BUD**
```bash
flight-deals search \
  -c seaside \
  --date-from 2026-07-06 --date-to 2026-07-10 \
  --return-from 2026-07-12 --return-to 2026-07-18 \
  --connections \
  --max-ground-minutes 240 \
  --fresh
```

**Goal: Italian hidden gems under €150 roundtrip**
```bash
flight-deals search -c italian-gems \
  --date-from 2026-07-07 --date-to 2026-07-09 \
  --return-from 2026-07-14 --return-to 2026-07-16 \
  --max-price 150 --connections
```

**Goal: Find price drops on a specific route**
```bash
flight-deals collect -c italian-gems --date-from 2026-07-01 --date-to 2026-07-31
flight-deals history-stats --destination CTA
flight-deals track --destination CTA --date-out 2026-07-08 --threshold 12
```

## Tips to Stay Creative & Unstuck
- Always use date **windows**, not single dates.
- Run the same search with and without `--connections`.
- After a search, immediately run `history-stats` on the interesting routes.
- Keep a list of "promising" categories and rotate them.
- When Apify is available, `--connections` becomes much more powerful.
- Use `--sort-by efficiency` when ground time matters.
- Cache can lie — use `--fresh` liberally during active hunting.

## Quick Reference Cheat Sheet
- Broad hunt: wide dates + `--connections` + category
- Precision: narrow windows + `--max-price` + specific roundtrip
- Drop hunting: `collect` → `history-stats` → targeted `search` with `--history-window`
- Stuck? Widen dates → add connections → --fresh → change category

This skill makes the tool extremely flexible. Combine flags differently every time and you will surface deals that single-strategy searches miss.

**Loaded as skill**: You can reference this document when the user asks for flight deals searches. Always propose 2-3 different flag/strategy combinations before settling on one.
