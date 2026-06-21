# Flight Deals Tracker — Implementation Plan (Updated)

**Update Date**: 2026-06-21
**New Focus**: Apify Integration for Connections (Phase 6)

## Phases Completed
- Phase 0-5: Foundation, Providers (Ryanair/Wizz), Registry, Orchestrator, Config/Cache, Hermes skill, Connections registry logic.

## Phase 6: Apify Multi-Source Provider for Connections (Current)

**Objectives**
- Add cheapest multi-airline support using Apify (~$0.0003/search).
- Turn `--connections` into real 1-stop / virtual interlining results (Google Flights + Kiwi data).
- Keep direct LCC searches free and primary.
- Make fully optional (no key = no Apify calls).
- Strong caching + cost warnings.

**Tasks**
1. Update RESEARCH.md, DESIGN.md, PLAN.md (this doc) with Apify details.
2. Extend `FlightDealsConfig`:
   - apify_token, apify_actor_id, apify_enabled, apify_cache_ttl_hours.
3. Enhance `FlightDeal` model:
   - stops: int
   - source_details: dict
   - booking_url: Optional[str]
4. Implement `providers/apify.py` (TDD):
   - Class ApifyProvider.
   - Uses requests + Apify run API.
   - Method: get_cheapest_flights(...) returning normalized deals.
   - Graceful degradation if no token.
5. Update `DealOrchestrator.search_by_category`:
   - When connections=True, also query ApifyProvider.
   - Merge results intelligently.
6. Update CLI `search` command:
   - Better table columns for stops and detailed source.
   - Warning when Apify is used (cost).
7. Enhance cache.py for Apify-specific TTL.
8. Add `tests/test_apify.py` (mocked API responses).
9. Update:
   - data/config.example.json
   - README.md
   - ~/.hermes/skills/travel/flight-deals/SKILL.md
   - scripts/daily_track.py (optional broad connections run)
10. Manual test with placeholder (dry-run mode).
11. Git commit with clear message.

**Deliverables**
- Working `flight-deals search --category european-islands --connections --date-from ...` that can return multi-source results when token is set.
- All tests passing.
- Clear docs on cost and configuration.

**Verification**
- Without token: falls back to current behavior, no errors.
- With mock token: returns deals with stops > 0 and source containing "apify".
- Caching prevents repeated paid calls.
- CLI output clearly labels connection deals.

**Guiding Rules**
- TDD: Write failing test → implement → pass.
- Optional & safe: Never require Apify token.
- Cost-aware: Cache aggressively.
- Reuse existing models/orchestrator patterns.

---

## Future Phases (Post 6)
- Phase 7: Advanced history analysis + price drop alerts using Apify data.
- Phase 8: Distance / travel-time filters using lat/lon.
- Polish + full Hermes cron integration for connections.