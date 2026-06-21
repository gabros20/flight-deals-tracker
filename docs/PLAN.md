# Flight Deals Tracker — Implementation Plan (Updated)

**Update Date**: 2026-06-21
**Focus**: Phase 7 Enhancements - Ground Transport Efficiency Additions (Option A)

## Phases Completed
- Phase 0-6: Foundation, Providers (Ryanair/Wizz/Apify), Registry, Orchestrator, Config/Cache, Hermes skill, Connections + Apify.
- Phase 7 Base: GroundTransport, GroundLeg, basic enrichment in orchestrator/CLI, haversine + OSRM + Transitous.

## Phase 7 Additions: Smarter Efficient Searching (Current Task)

**Objectives**
- Make ground calculations realistic: only apply ground transport for reasonable short/medium distances (e.g. <400km between hub airports).
- Separate air flight time from ground time properly (use deal.duration_minutes when available).
- Precompute ground data for speed and offline use on common BUD hub pairs.
- Add user controls: --max-ground-minutes filter, --ground-prefer mode.
- Add efficiency scoring for better ranking (total time + price efficiency).
- Improve orchestrator to support sorting by total time / efficiency.
- Ensure integration with Apify-sourced connection deals.
- Keep free, cached, local.

**Detailed Tasks (TDD-driven)**
1. Update RESEARCH.md + DESIGN.md with additions for smart filtering, precompute, efficiency scoring, CLI flags.
2. Enhance `src/flight_deals/ground.py`:
   - Add `is_reasonable_ground_distance()` (max 400km).
   - Add `max_distance_km` param to get_ground_options.
   - Improve `estimate_total_connection_time` to accept/ use air_duration_minutes.
   - Add `compute_efficiency_score(price, total_minutes)` helper.
3. Create/populate `data/ground_transfers.json` with precomputed values for common pairs (BUD-VIE, BUD-MUC, VIE-FRA, etc. + sample island ground).
4. Add precompute helper function + optional script.
5. Update models if needed (add efficiency fields?).
6. Update `DestinationRegistry` to support preloaded ground data + max distance.
7. Update `DealOrchestrator.search_by_category`:
   - Accept max_ground_minutes, ground_prefer.
   - Smarter enrichment: only if reasonable distance + use real air duration.
   - Support sorting by "total_time" or "efficiency".
8. Update `cli.py` search command:
   - New options: `--max-ground-minutes 120`, `--ground-prefer driving|public|any`, `--sort-by price|total-time|efficiency`.
   - Pass flags to orchestrator.
   - Improve table with ground mode and filter results.
9. Add/update `tests/test_ground.py` and integration tests for new logic/filters.
10. Update config.py for defaults (max_ground_minutes=180, ground_prefer="any").
11. Update Hermes skill, README, daily_track script to mention new flags.
12. Full pytest run + manual BUD connections test with new flags.
13. Git commit with detailed message.

**Deliverables**
- `flight-deals search ... --connections --max-ground-minutes 120 --ground-prefer public --sort-by total-time` produces realistic, filterable, ranked results.
- Precomputed ground data loaded fast.
- Efficiency score visible or used in ranking.
- All tests passing (30+).
- Docs reflect the additions.

**Verification Steps**
- Ground legs only shown for pairs <400km (e.g. BUD-VIE yes, VIE-PMI no or minimal).
- Total time = air_duration (if present) + ground + buffer.
- --max-ground-minutes filters out bad connections.
- Precompute loads without API calls.
- Apify + ground works together.
- No breaking changes to direct searches.

**Guiding Rules**
- TDD: tests before or alongside changes.
- Realism first: prevent absurd totals.
- User control via CLI flags.
- Precompute for performance.
- Reuse patterns from Apify integration (optional, cached, graceful).
- Local only.

---

## Future Phases (Post 7)
- Phase 8: Full price-per-hour ranking + charts.
- Phase 9: History + alerts with ground-adjusted prices.
- Self-hosted routers + more GTFS integration.
- Destination planning sub-tool (if needed).

## Completed: Multi-Airport Self-Transfer Hubs (Post Phase 7)

Added support for calculating connections via cities with multiple airports:
- Istanbul (IST/SAW), Milan (BGY/MXP), London (STN/LGW/LTN), Rome (CIA/FCO), Paris (BVA/CDG), Brussels (CRL/BRU), Warsaw (WAW/WMI).
- These are now included in get_connection_hubs and get_reachable_with_connections when --connections.
- GroundTransport automatically calculates realistic short ground legs between them (e.g. 79min BGY-MXP, 69min IST-SAW).
- Precomputed data and registry methods support this.
- Orchestrator enriches deals with ground_leg for these pairs.
- This enables efficient BUD -> multi-airport-hub -> Canary/Madeira/Islands routes with proper total time and efficiency scoring.

## Phase 8: Comprehensive Multi-Airport Connection Engine + Full Improvements (User: "all")

**Date**: 2026-06-21
**Goal**: Deliver real value from multi-airport self-transfers + improve all major connection-related areas.

### Areas to Improve (all requested)
1. Real self-transfer deal generation (generate composite BUD → entry + ground + exit → island deals)
2. Ground enrichment logic (make it trigger for hub-style and multi-airport cases, not only direct close pairs)
3. Full itinerary output (show complete path in CLI and models)
4. Reachability + KNOWN_DIRECT_ROUTES (wire new multi-airport airports properly)
5. Apify + multi-leg usage (use Apify for better connection pricing when available)
6. Price history & tracking (support composite routes)
7. Ground data quality (better precompute, notes, public transit for key pairs)
8. CLI / UX polish (better table for connections, path display, more helpful output)

### Implementation Plan (TDD + incremental)
- Update models: Add connection_path or legs for full itinerary.
- Registry: Expand KNOWN_DIRECT_ROUTES for new multi-airport airports.
- Orchestrator: 
  - When connections=True, explicitly search to multi-airport entry points.
  - Build composite deals using sibling airports + ground.
  - Generalize ground enrichment.
- Ground: Improve precompute for multi-airport pairs.
- CLI: Enhance table for paths.
- History: Support composite routes.
- Apify: Better usage for connections.
- Tests + docs updates.

**Success Criteria**
- Realistic composite deals with ground + path info appear with --connections.
- All 8 areas see measurable improvement.
- Tests pass.
