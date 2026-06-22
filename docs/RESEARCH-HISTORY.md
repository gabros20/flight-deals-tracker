# Flight Deals Tracker - Historical Price Data Research & Design

**Date**: 2026-06-22 (second round + implementation phase)
**Goal**: Add robust historical price tracking, automatic comparison on search results, badges ("Best this month", "Great deal vs yearly avg"), own data collection, and support for composite routes.

## 1. Feasibility & User Value (Confirmed)
- **High value**: Users can immediately know if a price is a "great deal" vs historical norms for that route/date.
- Auto-compare route+date → badges in search results.
- Own data collection solves lack of public LCC data.
- Supports cron + Telegram alerts on price drops relative to history.

## 2. Public Datasets (Limited / Not Sufficient)
From web + Kaggle searches:
- Mostly US domestic (1993-2024 fares) or Indian routes.
- Expedia sample (dilwong/FlightPrices, 2022).
- **No usable large-scale Ryanair or Wizz Air historical fare datasets** publicly available.
- Reason: LCCs avoid GDS; data is competitive.

**Recommendation**: Primary = self-collected. Secondary = optional aggregator snapshots.

## 3. Sources & Tools for Collection (Best Options)
**Ryanair**:
- ryanair_timecapsule (excellent API reverse-engineer for bulk fares + booking API).
- @2bad/ryanair (TS) + cohaolain/ryanair-py (Python) — already partially used.
- ryantrak (GitHub Actions daily CSV logger with Selenium).

**Wizz Air**:
- wizzair-scraper (timetable endpoint for bulk fast).
- wizzpricehistory (Node).
- kovacskokokornel/wizzair-scraper.

**Multi-source**:
- Apify flight-price-scraper actors (Ryanair + Wizz + Google Flights).
- Travelpayouts Flight Data API (RapidAPI): price history, calendar, trends (48h user data + aggregates).

**Aggregators for quick context**:
- Google Flights price insights (via SerpApi or scrapers): price_history array, typical range.
- Kayak/Hopper graphs.

**X/Twitter consensus**: Build your own with the above scrapers/APIs. Google Flights for reference.

## 4. Implementation Architecture (Chosen Design)
**Storage**:
- Current: CSV (simple, already partially implemented).
- Recommended hybrid: Keep CSV for appends (easy git-friendly). Use DuckDB for fast analytics when queries grow (DuckDB can query CSV/SQLite directly). Or migrate to SQLite for better querying.
- For now: Enhance CSV store + in-memory pandas-like computations (pure Python for zero deps).

**Data Model**:
- PriceSnapshot (existing) + extensions for full deal info (connection_path, duration, source_details).
- In-memory or query-time: HistoricalComparison model with min, avg, percentiles, best_this_month, best_this_year, comparison_note.

**Collection**:
- CLI: `flight-deals collect --category european-islands --date-from ...` or per-route.
- Automatic: On `search` (optional flag or background), append current results to history.
- Cron: Use existing Hermes cron to run collect on key routes daily.

**Comparison**:
- On search results: For each deal (origin-dest-dep_date), compute stats over:
  - Same route last 30/90/365 days.
  - Same month/year.
- Badges: "Best price this month", "25% below avg", "Typical range €XX-YY".
- Support for composite routes via route_key or full connection_path hash.

**Integration Points**:
- Models: Extend FlightDeal.
- HistoryStore: Add `get_route_stats()`, `compute_badges()`.
- Orchestrator: After fetching deals, call history.enrich_deals(deals).
- CLI: Add column or notes for history context. New `history` subcommands.
- Providers unchanged (they return current prices).

**Risks & Mitigations**:
- Sparse data early on → Start with "first tracked" note; require 5+ points for "best" badges.
- API churn → Rely on our existing clients + Apify fallback.
- Storage growth → Simple retention (keep last 2 years or prune old).
- Composites → Store full legs or a route_signature.

## 5. Comparison to Alternatives
- Google Flights / Kayak: Good UI, no API for bulk local history, no custom alerts.
- Commercial APIs (Amadeus etc.): Expensive.
- Our approach: Free, local, deterministic, integrated with categories + connections + ground.

## 6. Prioritized Features for v1
1. Basic stats (min/avg) + badge display in search.
2. `collect` command + auto-log on track/search.
3. Support for connection composites.
4. `history stats` command.
5. Cron-friendly collection.
6. Percentiles + "best of month/year".

## Sources (Key Links from Research)
- ryanair_timecapsule: https://github.com/mbalos16/ryanair_timecapsule
- ryantrak: https://github.com/thomasdstewart/ryantrak
- wizzpricehistory, wizzair-scraper, Travelpayouts, SerpApi Google Flights Price Insights, Apify actors.
- X discussions confirming self-build for LCCs.

This completes the research round. Implementation follows in PLAN.md and code.
