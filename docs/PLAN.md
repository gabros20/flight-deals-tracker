# Flight Deals Tracker - Implementation Plan

**Goal**: Implement reliable round-trip support using the best findings from research.

## Phase 1: Documentation & Setup (Current)
- [x] RESEARCH.md updated with second round
- [x] DESIGN.md created
- [ ] PLAN.md created (this file)
- [ ] git commit docs

## Phase 2: Core Fix - farepy Integration
1. Add `farepy` to requirements
2. Create `src/flight_deals/providers/farepy_provider.py`
   - Implement `get_roundtrip_price(origin, dest, departure_date, return_date)`
   - Use `search_flights(..., return_date=...)`
   - Handle both Ryanair and Google Flights results
3. Update `orchestrator.py` to use FarepyProvider for round-trips
4. Add `--provider farepy` flag for testing

## Phase 3: Direct Ryanair API Fallback
1. Add direct `availability` endpoint call in `ryanair.py`
2. Implement `get_roundtrip_via_booking_api(...)`
3. Use when farepy returns no results

## Phase 4: Testing & Validation
- Test with real BUD → Italian/Greek seaside routes for July 2026
- Compare results against Google Flights manually
- Verify round-trip prices are realistic (not one-way sums)
- Add `--fresh` testing

## Phase 5: Polish & Commit
- Update CLI help
- Update README with new provider
- Commit with clear message

**Success Criteria**: Tool returns accurate round-trip prices for at least 70% of requested short-stay European routes where flights exist.