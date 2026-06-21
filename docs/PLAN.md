# Flight Deals Tracker — Implementation Plan (Updated)

**Update Date**: 2026-06-21
**Focus**: Phase 7 - Ground Transport Efficiency (Option A: Integrated)

## Phases Completed
- Phase 0-6: Foundation, Providers (Ryanair/Wizz/Apify), Registry, Orchestrator, Config/Cache, Hermes skill, Connections registry + Apify multi-source.

## Phase 7: Ground Transport for Realistic Connections (Current - Option A)

**Objectives**
- Address the "missing unaccounted time" in connection searches.
- Calculate realistic **distance, travel time, and travel options** (driving, public transport) between airports/hubs.
- Integrate into `--connections` flows so users see total door-to-door estimates.
- Use free/low-cost sources: haversine (baseline), OSRM (driving), Transitous/MOTIS (public transit).
- Precompute for common European hubs from BUD.
- Add efficiency scoring and filters.
- Keep everything optional, cached, and local.

**Tasks**
1. Update RESEARCH.md, DESIGN.md, PLAN.md with ground transport findings and integration plan.
2. Add `src/flight_deals/ground.py`:
   - `GroundTransport` class.
   - Haversine distance.
   - OSRM public API for driving time/distance.
   - Optional Transitous integration (public API for public transport options).
   - Return structured `GroundLeg` options (mode, time_min, distance_km, cost_estimate, steps).
3. Enhance models:
   - Add `GroundLeg` Pydantic model.
   - Extend `FlightDeal` with optional `ground_leg: Optional[GroundLeg] = None` and `total_duration_minutes`.
4. Update `DestinationRegistry`:
   - `get_ground_options(origin_iata, dest_iata)` 
   - `get_connection_efficiency(origin, dest, flight_time_min)` 
   - Preload common ground data or compute on fly with cache.
5. Update `DealOrchestrator`:
   - When `connections=True`, enrich deals with ground legs between origin-hub or hub-dest.
   - Compute total effective time (air + ground + buffer).
   - Sort by price or by total_time.
6. Update CLI `search`:
   - New columns: Ground Time, Total Time, Ground Mode.
   - Options: `--max-ground-minutes`, `--show-ground-details`.
7. Add caching for ground results (leverage existing FlightCache or dedicated).
8. Add `tests/test_ground.py` (TDD first: haversine, OSRM mock, integration).
9. Update config for ground settings (osrm_base_url, transit_api, precompute).
10. Update data/ with `ground_transfers.json` sample for BUD hubs.
11. Update README, Hermes skill, daily_track.py.
12. Full tests + manual verification on BUD connections.
13. Git commit.

**Deliverables**
- `flight-deals search --category european-islands --connections --date-from ...` now shows realistic ground time and total door-to-door.
- Example output includes ground options for hub connections (e.g. BUD-VIE ground + VIE-PMI).
- All tests passing.
- Clear documentation on sources and limitations.

**Verification**
- Haversine matches known distances.
- OSRM calls return plausible driving times.
- Connections results include ground data without breaking direct searches.
- `--max-ground-minutes 60` filters unrealistic options.
- No external API keys required (public endpoints).

**Guiding Rules**
- TDD first.
- Free-first (OSRM + Transitous public).
- Cache aggressively (ground data is stable).
- Extend existing connection logic rather than replace.
- Local execution only.

---

## Future Phases (Post 7)
- Phase 8: Advanced efficiency scoring + price-per-hour.
- Phase 9: Full history analysis + alerts.
- Polish + precomputed matrices for all 58 airports.
- Optional self-hosted OSRM/Transitous for privacy/speed.