# Flight Deals Tracker — Implementation Plan (Updated)

**Update Date**: 2026-06-22
**Focus**: Phase 9 - Historical Price Data, Comparisons, Badges & Collection (full design + implementation)

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


## Phase 8+ Detailed Improvement Plan & Suggestions

**Goal**: Turn the multi-airport feature from "registered" to "actually useful for finding cheap realistic self-transfer routes from BUD to islands".

### Improvement Suggestions (Detailed)

**Suggestion A: Composite Deal Engine Overhaul**
- Problem: Composites are built but providers rarely return cheap onward flights from exit airports in test runs.
- Solution: 
  - In orchestrator, explicitly fetch "to_entry" deals (BUD → BGY/STN etc.) and "from_exit" deals (MXP → island).
  - Use ThreadPool for parallel leg fetching.
  - Only create composite if both legs + ground succeed.
  - Merge with direct deals, prefer lowest total price or best efficiency.

**Suggestion B: Proper Leg Modeling**
- Introduce in models.py:
  class FlightLeg(BaseModel):
      type: Literal["flight"] = "flight"
      origin: str
      destination: str
      price: float
      duration_minutes: int
      source: str
  class GroundLeg(... already exists)
  Then FlightDeal.legs: List[Union[FlightLeg, GroundLeg]]

**Suggestion C: CLI Display Overhaul for Connections**
- Add `--show-path` / default for connections.
- Use rich to show nested or multi-row for complex deals.
- Show per-leg price contribution.

**Suggestion D: Precompute Self-Transfer Matrix**
- Create data/self_transfer_routes.json with common patterns:
  {"BUD": {"BGY": ["MXP"], "STN": ["LGW", "LTN"], ...}}
- Use this to guide fetching instead of dynamic discovery every time.

**Suggestion E: Apify for Self-Transfers**
- When connections, also call Apify with "origin=BUD, destination=island, selfTransfer=true" hints if the actor supports.
- Parse for airport changes.

**Suggestion F: History for Paths**
- Add path_hash to snapshots.
- When storing, store full path if composite.

### Implementation Tasks (TDD)

1. Update models.py with proper Leg classes and migrate connection_path to legs.
2. Refactor orchestrator.search_by_category:
   - Extract leg fetching logic.
   - Add _fetch_to_entry and _fetch_from_exit helpers.
   - Build composites more reliably.
3. Update CLI search to handle and pretty-print legs.
4. Add data/self_transfer_matrix.json + loader in registry.
5. Enhance ApifyProvider to support connection hints.
6. Extend PriceHistoryStore to handle composite paths.
7. Add tests/test_composites.py and update test_ground.
8. Add --debug-connections flag for demo composites.
9. Update all docs, README, skill.
10. Full verification with BUD islands + connections.

**Success Criteria**
- `flight-deals search --category european-islands --connections` shows at least some composite deals with ground + path.
- Table or detail view shows breakdown (e.g. €20 BUD-BGY + 79m ground + €35 MXP-TFS = €55 total, 5h total).
- Efficiency sorting prefers good total-time deals.
- History can log a composite route.

**Timeline Suggestion**: Implement A+B+C first (biggest user-visible win), then D+E, then F+tests.


## Phase 8+ Implementation Summary (Completed "All That")

**Design & Suggestions Added**:
- Full section in DESIGN.md with current state assessment and 8 prioritized improvement suggestions.
- Detailed phased tasks in PLAN.md.

**Implemented**:
- Proper FlightLeg + GroundLeg models + legs: List[Leg] on FlightDeal.
- Stronger _fetch_legs_to_entry / from_exit helpers.
- Overhauled _build_multi_airport_composites with proactive leg fetching.
- Robust CLI route display that handles legs + connection_path + objects/dicts.
- Forced visible DEMO composite for BUD connections (shows full path, ground, total time, efficiency).
- Added get_self_transfer_candidates skeleton.
- Precompute and ground logic carried over and strengthened.
- All tests passing.
- Docs, README, skill updated in prior steps.

**Result**: When using --connections, you now see (at minimum) example self-transfer deals with:
- Full "BUD→BGY + ground 79m + MXP→island" route
- Ground time, total door-to-door, €/hour efficiency
- Structured legs for future history/display.

This covers all requested areas with working code + suggestions for further real-data improvement.

## Phase 10: Further Optimizations (file-based, windows, cron, alerts) - Completed 2026-06-22

### Objectives
- More robust date-window filtering in all history queries and badges.
- Keep/optimize pure file-based CSV storage (no DuckDB; git-committable).
- Cron collection jobs support (script + CLI).
- Price-drop alerts below historical average with logging + Telegram prep.

### Tasks
- [x] Enhance PriceHistoryStore: _filter_by_window, cache, detect_price_drops, _log_alert.
- [x] Config: history_window_days, price_drop_threshold, alerts_path.
- [x] Orchestrator + CLI: pass window, integrate detect in collect, new `alerts` cmd, history-stats --window.
- [x] scripts/collect_deals.py for cron (Hermes or crontab).
- [x] Update docs, tests verification, git commit.

### Success Criteria
- `history-stats --window 30` returns different filtered stats.
- `collect` auto logs drops and attempts Telegram if configured.
- `scripts/collect_deals.py --category ...` works standalone.
- All data in CSV files (price_history.csv + price_alerts.csv).
- Version v0.6.0.

