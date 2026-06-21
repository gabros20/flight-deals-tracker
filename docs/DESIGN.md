# Flight Deals Tracker — High-Level Architectural Design (Updated v0.5)

**Update**: 2026-06-21 — Apify Multi-Source Layer for Connections

## Core Strategy (unchanged)
"Smart Reuse + Thin Abstraction Layer"

## Updated Architecture with Apify

```
User / Hermes Skill
          │
          ▼
CLI / DealOrchestrator
          │
    ┌─────┴─────┐
    │           │
    ▼           ▼
Destination   Provider Layer
Registry      • RyanairProvider (direct LCC, fast)
              • WizzProvider    (direct LCC)
              • ApifyProvider   (multi-source: Google Flights + Kiwi + LCCs)
                                  → used for connections / virtual interlining
          │
          ▼
   History, Cache, Notifier
```

### Provider Layer Details
- **Abstract interface** (implicit via shared methods):
  - get_cheapest_flights(origin, date_from, date_to, destination=None)
  - Returns List[FlightDeal]
- **RyanairProvider & WizzProvider**: Primary for direct. Fast, accurate, free.
- **ApifyProvider** (new):
  - Thin wrapper around Apify API.
  - Configurable actor ID + token (from env / config file).
  - When token absent: gracefully skips or warns.
  - For connections: leverages Google Flights / Kiwi results which return stops, segments, isSelfTransfer.
  - Results normalized to FlightDeal + extra fields (stops, source_details, booking_url).
  - **Cost control**: Longer cache TTL (12-24h recommended), only invoked on --connections.

### FlightDeal Model Extensions
- Added:
  - stops: int = 0
  - source_details: dict (e.g. {"cheapestSource": "google_flights", "prices": {...}})
  - booking_url: Optional[str]

### Orchestrator Changes
- When connections=True:
  - Use registry for candidates (as before).
  - Call Ryanair + Wizz for directs.
  - **Additionally** call ApifyProvider for richer multi-airline + 1-stop options.
  - Merge + dedup by (origin, dest, date, price), prefer lowest.
- Parallel execution preserved.

### Caching
- Apify results cached separately with longer TTL (cost-sensitive).
- Invalidation by route/date.

### Config
- New fields:
  - apify_token: Optional[str]
  - apify_actor_id: str = "makework36/flight-price-scraper"
  - apify_enabled: bool = True
  - apify_cache_ttl_hours: int = 12

### Hermes Skill Integration
- Expose `search_deals(..., connections=True)` which triggers Apify path when configured.

This keeps direct LCC searches free/fast while adding powerful connection support at minimal cost.
