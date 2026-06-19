# Flight Deals Tracker — High-Level Architectural Design (v0.2)

**Date**: 2026-06-19
**Status**: Architectural design complete (implementation-agnostic)
**Goal**: Design a maintainable, extensible system that reuses the best existing components while providing a clean abstraction for broad category-based European flight deal discovery, price tracking, and Telegram alerts.

## 1. Design Philosophy & Strategy

**Core Strategy: "Smart Reuse + Thin Abstraction"**

After inspecting multiple working implementations (`cohaolain/ryanair-py`, `@2bad/ryanair`, `kovacskokokornel/wizzair-scraper`, `ryantrak`, Apify actors, and X-shared Telegram bot patterns), the optimal approach is:

- **Reuse proven clients** rather than building from scratch.
  - Ryanair: Base on `cohaolain/ryanair-py` (clean Python, `farfnd/v4` endpoints, retry logic, good types) + reference patterns from `@2bad/ryanair` (modular `airports`/`fares`/`flights` separation).
  - Wizz Air: Base on timetable + search POST patterns from `kovacskokokornel/wizzair-scraper` with dynamic version detection.
  - Fallback: Apify actors (`maximedupre/ryanair-scraper` and similar multi-source actors) when direct scraping is blocked.
- **Do not fork** the existing repos. Instead, wrap them behind stable interfaces.
- **Piggyback on real-world patterns** from X threads (Node.js/Python + Telegram polling bots, SQLite history, frequent calendar checks).
- **Emphasize resilience** over raw speed: version detection, provider fallbacks, rate limiting, and clear separation of concerns.

This yields a **hybrid architecture** that is:
- Low maintenance (leverage existing working code).
- Extensible (easy to add EasyJet, new categories, or new providers).
- User-friendly for the requested broad semantic searches.

## 2. High-Level System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      User / Hermes Skill                     │
│   (natural language: "European islands under €150 from BUD") │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    CLI / Orchestrator Layer                   │
│  • Typer CLI commands                                        │
│  • DealOrchestrator (category expansion, filtering)          │
│  • SearchCoordinator                                         │
└──────────────────────────────┬──────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  Destination     │  │   Provider       │  │   History &      │
│  Registry        │  │   Layer          │  │   Alerting       │
│                  │  │                  │  │                  │
│ • JSON/SQLite    │  │ • RyanairProvider│  │ • PriceHistory   │
│ • Tags &         │  │ • WizzProvider   │  │   Store          │
│   metadata       │  │ • ApifyFallback  │  │ • AlertEngine    │
│ • Geo filtering  │  │ (adapter)        │  │ • TelegramSender │
└──────────────────┘  └──────────────────┘  └──────────────────┘
          │                    │                    │
          └────────────────────┴────────────────────┘
                               │
                               ▼
                    External Services
              (Ryanair API, Wizz API, Telegram, Apify)
```

### 2.1 Core Layers (High-Level)

**1. Destination Registry (Data Foundation)**
- Single source of truth for all Ryanair + Wizz airports.
- Rich metadata: IATA, city, country, lat/lon, is_ryanair_base, is_wizz_base, tags[] (european-islands, seaside, italian-gems, shopping, etc.), avg_flight_duration.
- Supports queries like:
  - "all european-islands reachable from BUD"
  - "seaside cities within 3h flight or €120"
- Populated initially from Ryanair/Wizz active airport endpoints + manual curation for tags.
- Can be extended with user preferences (home airports, max travel time).

**2. Provider Layer (Abstraction over External APIs)**
- `FlightProvider` abstract interface with methods:
  - `get_cheapest_per_day(origin, dest, month)`
  - `get_available_dates(origin, dest)`
  - `search_fares(origin, dest, date_range, max_price=None)`
  - `get_destinations_from(origin)`
- Concrete implementations:
  - `RyanairProvider` — wraps `cohaolain/ryanair-py` logic + `@2bad/ryanair` patterns (modular fares/airports).
  - `WizzProvider` — custom requests client based on `kovacskokokornel` timetable approach + dynamic version handling.
  - `ApifyProvider` (optional) — thin wrapper around Apify actors for resilience.
- Key design: Providers are **stateless adapters**. All version handling, headers, retries live here.
- Benefit: Easy to swap or add providers without touching higher layers.

**3. Domain / Orchestration Layer**
- `DealOrchestrator`:
  - Expands user intent ("European islands") → list of candidate destinations via registry tags + filters.
  - Coordinates parallel searches across providers.
  - Applies business rules (price caps, travel time, one-way vs return, preferred days).
- `SearchCoordinator` / `Comparator`:
  - Runs multi-route, multi-date searches.
  - Normalizes results into common `FlightDeal` model.
  - Finds best options across Ryanair vs Wizz.

**4. Tracking & Alerting Layer**
- `PriceHistoryStore`: Append-only log (CSV or SQLite) of `(timestamp, route, date, price, currency, source)`.
- `AlertEngine`:
  - Detects significant drops (percentage or absolute).
  - "Best time to buy" signals based on historical patterns.
  - Integrates with cron/scheduler.
- `TelegramNotifier`: Sends rich messages with deal details + direct booking links (reuses patterns from X-shared bots).

**5. Interface Layer**
- Typer-based CLI (`flight-deals search ...`, `flight-deals track ...`).
- Hermes skill wrapper for natural language interaction.
- Optional web dashboard later (FastAPI + HTMX).

## 3. Key Data Models (Conceptual)

- `Airport` — IATA, name, coords, tags, providers[]
- `FlightDeal` — origin, dest, departure_date, return_date?, price, currency, source, deep_link, duration
- `PriceSnapshot` — timestamp, deal_id or route+date, price, source
- `SearchQuery` — origin(s), category or destination_tags, date_range, max_price, passengers

## 4. Data Flow Examples

**Broad Category Search Flow**
1. User: "best European islands deals from Budapest next month under €150"
2. Orchestrator → Registry: expand "european-islands" + filter by origin=BUD
3. For each candidate route → Provider(s) → cheapest fares
4. Comparator ranks results
5. Return top N + history comparison

**Price Tracking + Alert Flow**
1. Cron triggers `track --route STN-BGY --threshold 15%`
2. Provider fetches current price
3. HistoryStore appends snapshot
4. AlertEngine compares to previous N days
5. If drop detected → TelegramNotifier sends alert

## 5. Resilience & Extensibility Design

- **Version Handling**: Centralized in providers (dynamic discovery for Wizz, client-version refresh for Ryanair on 409).
- **Fallbacks**: Provider chain (Ryanair direct → Apify → graceful degradation).
- **Rate Limiting & Politeness**: Built into providers + global coordinator.
- **Extensibility Points**:
  - New provider = implement `FlightProvider`
  - New category = add tags to registry + query logic
  - New notification channel = new Notifier class
- **Testing Strategy**: Mock providers for unit tests; live integration tests on key routes.

## 6. Non-Functional Requirements Addressed
## 6. Non-Functional Requirements Addressed
- **Maintainability**: Thin wrappers around proven code → minimal custom logic.
- **Reliability**: Multiple data sources + history tracking.
- **Usability**: Natural language via skill + powerful CLI.
- **Cost**: Mostly free (direct APIs + optional cheap Apify runs).
- **Privacy**: All data local + user-controlled.
This design maximizes reuse of working components (`cohaolain/ryanair-py`, wizzair-scraper patterns, X Telegram bot patterns, Apify) while creating a clean, category-aware system tailored to the user's broad European island/seaside/gem/shopping searches and long-term price tracking needs.
Next: PLAN.md (phased implementation roadmap).

## 7. Second Round Design Refinements (Deeper Inspection)

After re-cloning and deeper file inspection of the source repositories, several valuable patterns and missed details were identified and folded into the architecture:

### Additional Insights from Inspected Code

- **cohaolain/ryanair-py**:
  - `SessionManager` pattern (bootstrap cookies by visiting main site first) is simple and effective for maintaining sessions.
  - Clean `dataclass` models (`Flight`, `Trip`) with `departureTime`, `flightNumber`, `price`, `currency`, full origin/destination names — excellent for normalization.
  - Uses `backoff` for retry logic on queries.
  - Supports rich parameters (destination_country, time windows, max_price, custom_params).

- **@2bad/ryanair** (TypeScript reference):
  - Strong Zod schemas for `Fare`, `CheapestFares`, `RoundTrip`, `Price` — inspires use of Pydantic models in Python for validation and type safety.
  - `findDailyFaresInRange` implementation (split by month → parallel `getCheapestPerDay` → flatten) is highly efficient for broad date-range searches.
  - Modular separation (`airports/`, `fares/`, `flights/`) and MCP tools (agent-friendly endpoints) are valuable for future Hermes skill integration.

- **ryantrak**:
  - Simple, proven CSV schema for price history (`timestamp_utc, origin, destination, departure_date, arrival_date, price, currency`).
  - GitHub Actions + dataclass `SearchConfig` pattern for scheduled tracking.

- **Wizz Air patterns** (from earlier scraper analysis):
  - Need explicit support for bundle types (BASIC, WIZZ GO / MIDDLE, PLUS) and WDC pricing in the `WizzProvider`.

### Refinements Integrated into the Design

1. **Provider Layer Enhancements**:
   - `RyanairProvider` will incorporate `SessionManager`-style cookie bootstrap and `backoff` retry.
   - Add Pydantic models mirroring the clean `Flight`/`Trip` dataclasses and Zod `Fare` schemas.
   - Implement `find_daily_fares_in_range` style logic for efficient category-wide date scanning.

2. **History Store**:
   - Adopt the exact CSV column structure from `ryantrak` as the initial simple storage format (easy to migrate to SQLite later).

3. **Wizz Provider**:
   - Explicitly model bundle types and WDC flag in result objects.

4. **Future-Proofing**:
   - Reserve space for MCP-style tool exposure (from 2bad) so the system can later be exposed as a Hermes-native agent tool.

5. **Error & Resilience**:
   - Centralize 409 "client-version" refresh logic (Ryanair) and dynamic version detection (Wizz) inside the Provider layer with clear logging.

These refinements make the architecture even more robust and directly informed by production-grade code patterns from the inspected tools.

Next: PLAN.md (phased implementation roadmap).