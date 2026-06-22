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

## Ground Transport Layer (Added in v0.5+ for Option A)

**New Component**: `GroundTransport`
- Calculates realistic ground legs between airports (driving via OSRM, public transit via Transitous/MOTIS).
- Returns `GroundLeg` objects: mode, duration_minutes, distance_km, estimated_cost, notes.
- Integrated into DealOrchestrator for connections.
- When connections=True: for hub-based paths, add ground time between arrival airport and next departure or final dest.
- Efficiency: total_time = flight_duration + ground + buffer.
- Precomputed static data for common pairs + on-demand.

**Data Flow Update**
```
Search (connections=True)
  → Registry: reachable with hubs
  → Providers: air deals (Ryanair/Wizz/Apify)
  → GroundTransport: enrich with ground options for hub transfers
  → Orchestrator: merge, compute total_door_to_door, filter/sort
  → CLI: display Ground Time + Total Time
```

**Benefits**
- Makes --connections results actionable (users see real total time).
- Free/low-cost (OSRM + Transitous public endpoints).
- Extensible (add more modes, self-hosted routers later).

This directly solves the "unaccounted time and travel options" gap identified in research.

## Ground Transport Additions (Phase 7 Enhancements)

**Smart Filtering**
- Ground legs only applied when haversine distance < 400 km (configurable).
- Prevents using driving estimates for long-haul "connections".
- For long air segments, ground is only used for hub airport transfers (e.g. arrival at one airport, depart from another close one, or to final city).

**Precomputed Data**
- `data/ground_transfers.json`: Static matrix for common pairs (BUD + hubs).
- Loaded at startup for instant results + offline.
- Falls back to live OSRM/Transitous only for new pairs.

**Efficiency Scoring**
- `efficiency_score = price / (total_minutes / 60)` (lower is better: € per hour total door-to-door).
- Or pure total_time ranking.
- Orchestrator can sort by "efficiency" or "total-time".

**CLI Controls**
- `--max-ground-minutes`: Filter deals where ground > threshold.
- `--ground-prefer`: driving | public | any.
- `--sort-by`: price (default) | total-time | efficiency.

**Orchestrator Flow Update**
1. Fetch air deals (LCC + Apify).
2. For connections: compute ground only if reasonable distance.
3. If deal has `duration_minutes`, use it for air time.
4. total_duration = air + ground + buffer.
5. Filter + sort per user flags.
6. Attach ground_leg and efficiency.

**Data Model**
- GroundLeg remains.
- FlightDeal gains optional efficiency_score.
- Registry can preload from ground_transfers.json.

This makes connection searches "efficient" by surfacing realistic options and allowing users to optimize for time or value.


## Phase 8: Multi-Airport Self-Transfer Engine

**Key Design**:
- FlightDeal gains optional connection_path: List[dict] to represent full itineraries (flight leg + ground leg + flight leg).
- Orchestrator has a new _build_composite_deals method that:
  1. Gets multi-airport entry airports reachable from origin.
  2. Fetches deals to those entry airports.
  3. For interesting destinations, fetches from sibling exit airports.
  4. Attaches GroundLeg between entry and exit.
  5. Creates composite FlightDeal with full path and adjusted total time/price.
- Ground enrichment is generalized: always consider short ground for multi-airport pairs even if not the direct deal pair.
- Registry's MULTI_AIRPORT_CITIES drives the logic.
- CLI renders path when present (e.g. "BUD→BGY + 79m ground + MXP→TFS").
- Backward compatible: direct deals unchanged.


## Improvement Suggestions for Multi-Airport Self-Transfer Engine (Phase 8+)

### Current State Assessment (as of latest implementation)
- Multi-airport cities are registered and ground calculations work.
- `_build_multi_airport_composites` exists but produces few/no results in practice (provider data for future dates + limited LCC routes from exit airports).
- Ground enrichment triggers rarely for island destinations.
- `connection_path` is basic dict list; no rich leg model.
- CLI shows basic table; no per-leg breakdown or full itinerary view.
- Apify is used but not optimized for self-transfer detection.
- History and tracking not yet aware of composite paths.

### Key Improvement Suggestions

1. **Stronger Composite Generation Strategy**
   - Proactive fetching: Always query Ryanair/Wizz/Apify for BUD → entry_airport (e.g. BGY, STN, SAW) separately when connections=True.
   - Then query exit_airport → target_island.
   - Combine only if both legs exist + ground is reasonable.
   - Add fallback to "virtual" self-transfer deals when Apify returns `isSelfTransfer=True` or similar.

2. **Richer Leg Model**
   - Create `Leg` base + `FlightLeg` and `GroundLeg` subclasses (or use Pydantic models).
   - `FlightDeal` should have `legs: List[Leg]` instead of (or in addition to) flat fields + `connection_path`.
   - This enables proper serialization, history, and display of price/time per segment.

3. **Dedicated Self-Transfer Mode**
   - Add `--self-transfer` or enhance `--connections` with `multi_airport_only` option.
   - Registry should have `get_self_transfer_candidates(origin, category)` that prioritizes multi-airport hubs.

4. **Improved CLI Output for Complex Routes**
   - When a deal has multiple legs, show:
     - Summary row
     - Or use `--detail` flag for expanded view (leg1 price/time, ground, leg2 price/time, total).
   - Add columns or sections for "Via" (e.g. "Via Milan (BGY-MXP)").

5. **Apify Optimization for Self-Transfers**
   - When calling Apify, pass hints like "self transfer" or specific multi-airport pairs.
   - Parse Apify results for `isSelfTransfer` or airport change indicators.
   - Prefer Apify for composite routes when available (more realistic interlining/self-transfer data).

6. **Ground + Total Time Realism**
   - Always separate air time from ground.
   - Add "buffer" config and "comfort factor" for efficiency scoring (e.g. penalize very short ground buffers).
   - Precompute a full "BUD multi-airport matrix" (all reasonable entry/exit pairs + estimated total for common islands).

7. **History & Tracking for Composites**
   - Extend `PriceHistoryStore` to store `path_signature` (e.g. "BUD-BGY-ground-MXP-TFS").
   - Track price changes on the full composite, not just single legs.
   - In cron/alerts, report "good self-transfer via Milan appeared at €XX".

8. **Testing & Visibility**
   - Add synthetic test deals for multi-airport paths.
   - CLI flag `--debug-connections` to force some composite examples.
   - Better error messages when no composites found.

### Recommended Priority Order for Implementation
High impact / feasible now:
- 1 + 2 + 4 (better composites + model + display)
- 3 + 6 (dedicated mode + realism)
- 5 + 7 (Apify + history)
- 8 (testing)

This will make `--connections` actually surface usable BUD → Canary/Madeira self-transfer deals with accurate total time and breakdown.


## Phase 9: History & Price Comparison Layer (Added 2026-06-22)

**New Component**: PriceHistoryStore (enhanced) + HistoricalComparison model

- CSV append for snapshots (supports full connection_path for composites).
- On-the-fly stats: min/avg/percentiles, best-month/year detection.
- Orchestrator enrichment: every search result gets `historical_comparison` + `comparison_note`.
- CLI: "History" column + badges in notes (e.g. "Best this month! | Hist: min €48 avg €92 (n=12)").
- Collection: `collect` command snapshots current category results.
- `history-stats` for quick aggregates.

**Data Flow**:
Search → Providers/Composites → Ground → History.enrich_deals() → CLI display with badges

**Benefits**:
- Immediate "is this a good deal?" context without external tools.
- Own data beats public datasets for LCCs.
- Ready for cron alerts on "below historical average".

See docs/RESEARCH-HISTORY.md and PLAN.md Phase 9 for details and sources.

## Phase 10 Additions: Date Windows, File-based Optimizations, Cron & Alerts

**Date-window filtering**
- All stats/enrich use departure_date parsing + cutoff = today - window_days.
- Improves "best this month" accuracy and allows user-controlled comparisons (e.g. summer 2026 vs full year).

**File-based storage (explicit requirement)**
- CSV primary (append-only, easy git).
- In-memory _cached_rows for query speed without loading disk every time.
- Separate price_alerts.csv for audit of drops.
- No external DB; pure stdlib + pydantic.

**Cron collection**
- scripts/collect_deals.py is self-contained CLI script for scheduling.
- Example Hermes cron: use cronjob create with prompt calling the script or flight-deals collect.
- Supports all existing flags (--connections etc).

**Price-drop alerts**
- Threshold-driven (config + param).
- Auto-called from collect.
- Structured alerts + notifier.send_price_alert.
- Can be extended for "below avg for this route/date window".

All changes maintain compatibility with multi-airport connections and ground transport.
