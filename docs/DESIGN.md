> **Historical, pre-rebuild.** This document predates the agent-first rewrite
> (`docs/UPGRADE-PLAN.md`) and proposes a `farepy`/Apify-based architecture
> that was never built this way — the shipped system has no `farepy`
> provider and no Apify dependency (free stack only). Kept for historical
> reference only; current design lives in `docs/SEARCH-DESIGN.md` and
> `docs/CONTRACT.md`.

# Flight Deals Tracker - Design Document

**Version**: 2026-06-22  
**Focus**: Fixing reliable round-trip pricing + broad category searches from BUD

## 1. Core Problem Being Solved
The current `ryanair-py` library returns empty results for many short-stay round-trips in July 2026. We need a more robust way to get accurate round-trip prices.

## 2. Chosen Solution (Second Research Round)
**Primary approach**: Integrate `farepy` (thorwhalen/farepy) as the main round-trip provider.
- Native `return_date` support
- Falls back to Google Flights when Ryanair direct data is missing
- Normalized output

**Secondary approach**: Direct Ryanair Booking API (`/api/booking/v4/.../availability` with `TripType: "ROUNDTRIP"`) as a fast local fallback.

**Tertiary**: Apify multi-source actor only for connections or when local methods fail.

## 3. Architecture

### Providers (src/flight_deals/providers/)
- `ryanair.py` → Keep for one-way + cheap direct flights (fast)
- `farepy_provider.py` (new) → Primary round-trip provider
- `wizz.py` → Timetable-based bulk search
- `apify_provider.py` → Optional, connection-aware

### Orchestrator Changes
- When user requests round-trip (return dates provided), prefer `FarepyProvider`
- Fall back to direct Ryanair API if farepy fails
- Combine results and deduplicate by price

### Data Flow
1. User request (category or specific route + dates)
2. Destination loader expands categories
3. Provider(s) fetch round-trip offers
4. Results ranked + cached (15 min TTL)
5. Price history logged to CSV
6. Alerts triggered on good deals

## 4. Key Design Decisions
- Prefer local Python libraries over hosted services for cost and speed on directs
- Use Apify only when `--connections` or for Wizz coverage
- Keep all data in local CSV/JSON files
- Support `--fresh` flag to bypass cache during testing

## 5. Non-Goals (for this phase)
- Full self-transfer connection logic (postponed)
- Telegram alerts (already working)
- Category expansion (already implemented)