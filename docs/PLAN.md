# Flight Deals Tracker — Implementation Plan (Phased Roadmap)

**Date**: 2026-06-19
**Based on**: RESEARCH.md + DESIGN.md (hybrid reuse strategy)
**Goal**: Deliver a working CLI tool in 4–6 focused phases that reuses proven components, supports category-based searches, price tracking, and Telegram alerts.

## Guiding Principles
- **Reuse first**: Wrap `cohaolain/ryanair-py`, wizzair-scraper patterns, and Apify where helpful.
- **Incremental value**: Each phase ends with a usable artifact.
- **Keep it lean**: No over-engineering. Start with CSV history, direct API providers, simple Typer CLI.
- **Verification**: Every phase includes manual testing on real routes.

---

## Phase 0: Project Setup & Foundation (1–2 days)

**Objectives**
- Initialize clean Python project structure.
- Set up dependency management, linting, and basic testing.
- Create initial data models and configuration.

**Tasks**
1. Create `pyproject.toml` (or `requirements.txt`) with core deps:
   - `typer`, `rich`, `pydantic`, `requests`, `backoff`, `python-dateutil`
   - `pandas` (optional, for later history analysis)
2. Project layout:
   ```
   src/flight_deals/
       providers/
           ryanair.py
           wizz.py
       registry/
           destinations.py
       models.py
       cli.py
   data/
       destinations.json          # initial seed
       price_history.csv
   ```
3. Define core Pydantic models (`FlightDeal`, `Airport`, `PriceSnapshot`).
4. Add `.gitignore`, basic README, and git tags for phases.

**Deliverable**
- Runnable `flight-deals --help` skeleton.
- `data/destinations.json` skeleton with 20–30 sample airports + tags.

**Verification**
- `python -m flight_deals.cli --help` works.
- Models validate sample data.

---

## Phase 1: Ryanair & Wizz Providers (3–5 days)

**Objectives**
- Get reliable access to cheapest fares and availability from both airlines.

**Tasks**
1. **RyanairProvider**
   - Wrap `cohaolain/ryanair-py` logic (or port key parts).
   - Add `SessionManager`-style cookie bootstrap.
   - Implement: `get_cheapest_flights`, `get_cheapest_return_flights`, `find_daily_fares_in_range`.
   - Handle `client-version` refresh on 409 errors.
2. **WizzProvider**
   - Implement timetable + search POST client (dynamic version discovery).
   - Support WDC flag and bundle types (BASIC / WIZZ GO / PLUS).
   - Basic error handling and rate limiting.
3. Optional: Thin `ApifyProvider` wrapper for fallback.

**Deliverable**
- Working `providers/ryanair.py` and `providers/wizz.py` that can fetch real prices for known routes (e.g. BUD → ALC, STN → BGY).

**Verification**
- Run manual queries for 3–5 routes and confirm prices match airline websites.
- Log raw responses for debugging.

---

## Phase 2: Destination Registry + Basic Search (2–3 days)

**Objectives**
- Enable category-based destination discovery.

**Tasks**
1. Build `DestinationRegistry`:
   - Load from `destinations.json`.
   - Support filtering by tags (`european-islands`, `seaside`, `italian-gems`, `shopping`).
   - Add simple geo helpers (haversine distance, rough flight time estimate).
2. Implement basic `DealOrchestrator.search(category, origin, date_range, max_price)` that:
   - Expands category → candidate airports.
   - Calls providers in parallel.
   - Returns normalized `FlightDeal` list.

**Deliverable**
- Working category search: `flight-deals search --category "european-islands" --from BUD --max-price 150`

**Verification**
- Search returns relevant islands (Greek, Canary, Balearic, Sicilian, etc.).
- Results include price, dates, and source.

---

## Phase 3: Price History, Tracking & Telegram Alerts (4–6 days)

**Objectives**
- Add persistent tracking and notifications.

**Tasks**
1. **PriceHistoryStore**
   - Use the CSV schema from `ryantrak` (`timestamp_utc, origin, destination, departure_date, arrival_date, price, currency`).
   - Simple append + query last N snapshots.
2. **AlertEngine**
   - Detect price drops (>15% or absolute threshold).
   - "Best time to buy" signals based on recent history.
3. **TelegramNotifier**
   - Use `python-telegram-bot` or simple `requests` + bot token.
   - Send formatted deal + booking link messages.
4. Basic cron support via `APScheduler` or external GitHub Actions example.

**Deliverable**
- `flight-deals track --route STN-BGY --threshold 15% --telegram`
- Daily/periodic runs that log history and send alerts on drops.

**Verification**
- Run tracker for 2–3 days on a real route.
- Confirm CSV grows and Telegram messages arrive.

---

## Phase 4: Full CLI + Category Orchestration Polish (3–4 days)

**Objectives**
- Make the tool pleasant to use for the original request.

**Tasks**
1. Expand Typer CLI with rich tables, progress bars, and helpful defaults.
2. Add useful commands:
   - `search`, `track`, `history`, `destinations list --tag islands`
   - `--return`, `--flex-days`, `--currency`
3. Improve `DealOrchestrator` with better ranking (price + duration + tags).
4. Add simple export (CSV/JSON).

**Deliverable**
- Polished CLI that feels complete for daily use.

**Verification**
- End-to-end test: broad category search → pick a route → start tracking → receive alert.

---

## Phase 5: Hermes Skill Integration & Documentation (2–3 days)

**Objectives**
- Make the tool usable via natural language in the Hermes agent.

**Tasks**
1. Create `hermes-skill/flight-deals/` with:
   - `skill.md` describing capabilities.
   - Wrapper functions that call the CLI or import the orchestrator.
2. Update README with examples of both CLI and skill usage.
3. Optional: Expose a few MCP-style tools (inspired by `@2bad/ryanair`).

**Deliverable**
- Working Hermes skill so the user can say: "Find me the cheapest European island deals from Budapest next month under €150"

**Verification**
- Skill responds correctly in a test Hermes session.

---

## Overall Timeline & Milestones

| Phase | Focus                        | Estimated Time | Milestone Deliverable                  |
|-------|------------------------------|----------------|----------------------------------------|
| 0     | Setup                        | 1–2 days       | Runnable CLI skeleton                  |
| 1     | Providers                    | 3–5 days       | Real Ryanair + Wizz price fetching     |
| 2     | Registry + Search            | 2–3 days       | Category-based search works            |
| 3     | History + Alerts             | 4–6 days       | Tracking + Telegram notifications      |
| 4     | Polish CLI                   | 3–4 days       | Production-ready CLI                   |
| 5     | Hermes Skill                 | 2–3 days       | Natural language usage via agent       |

**Total**: ~15–23 days of focused work (can be done part-time).

## Risks & Mitigations

- **API breakage** (especially Wizz version changes): Dynamic version detection + Apify fallback in providers.
- **Rate limits / blocks**: Built-in sleeps, session reuse, optional proxies.
- **Data quality**: Always include direct booking links and disclaimer.
- **Scope creep**: Stick strictly to the phases above.

## Next Steps After Phase 5

- Optional: Add EasyJet or other LCCs.
- Optional: Web dashboard or price trend charts.
- Optional: Advanced analytics (best months, price prediction hints).

---

This plan delivers a **minimum lovable product** by the end of Phase 3 and a **complete, agent-integrated tool** by Phase 5, while staying true to the reuse-heavy, low-bloat philosophy defined in the design.

Ready to start Phase 0 when you are.