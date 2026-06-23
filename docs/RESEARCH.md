# Flight Deals Tracker - Research Document (Updated with Apify Integration)

**Project**: CLI tool + Hermes skill for tracking Ryanair, Wizz Air, and multi-airline flight prices (including connections via virtual interlining) across broad European categories.

**Latest Update**: 2026-06-21 - Added Apify-based multi-source for connections.

## Previous Research Summary (Ryanair, Wizz, Destinations)
[Keep original sections 1-10 from earlier versions...]

## 11. Apify for Multi-Airline & Connections (New Research Round)

**Why Apify?**
- Cheapest hosted API for multi-source flight search: **~$0.0003 per search** (~$2.50 / 1,000 results).
- One call pulls **7 sources in parallel**: Google Flights, Kiwi.com, Travelpayouts, Ryanair, EasyJet, Wizz Air, Norwegian.
- Built-in support for **virtual interlining / self-transfer** (exactly what we need for true 1-stop connections from BUD).
- Returns: bestPrice, per-source prices, segment details, layovers, baggage, booking links.
- HTTP only (no browser), fast (2-4s), deduplicated.
- Production-grade actor: `makework36/flight-price-scraper`.

**Pricing Details**:
- Pay-per-event, no subscription.
- New users: $5 free trial (thousands of searches).
- Perfect for our use case (category sweeps + daily tracking + occasional connection searches).

**Actor Usage**:
- Input examples include route/date params.
- Output includes `bestPrice`, `cheapestSource`, `prices` map, `segments`, `isSelfTransfer`.
- Can be called via Apify API: `POST https://api.apify.com/v2/acts/{actorId}/runs` with token.

**Integration Strategy**:
- Keep Ryanair + Wizz for fast, accurate **direct LCC** deals (often cheapest for directs).
- Use Apify **only when `--connections` flag is used** or for broader coverage.
- Heavy caching (12h+ TTL for Apify results) to control costs.
- Merge results: prefer lowest price, annotate source.

**Alternatives Considered** (why Apify wins on cost):
- LetsFG Developer API: $0.10–$0.50/search (10-100x more expensive).
- Kiwi Tequila: Free tier very restricted (B2B only in practice); affiliate model.
- farepy (local Google Flights): Near-free but less structured interlining support.
- Direct GDS: Expensive contracts.

**Recommendation**: Implement thin `ApifyProvider`. Make token optional. Use for connections and as fallback.

**Risks**:
- Cost control via cache + only use on connections.
- Actor may change; keep configurable actor ID.
- Results may include self-transfers (user must handle separate tickets).

## 12. Next Steps
- Design + Plan update for ApifyProvider.
- TDD implementation.
- Hermes skill enhancement.
- Config support for token (user will provide later).

## 14. Second Research Round (June 2026) — X Search + Fresh Web Findings

**Goal of this round**: Find better tools/libraries for reliable **round-trip** pricing (especially Ryanair) after the `ryanair-py` library returned empty results for July 2026 short stays.

### Key New Discoveries

**1. farepy (thorwhalen/farepy) — Strongest local candidate**
- New 2026 multi-source Python library.
- Supports `search_flights(..., return_date=...)` natively for round-trips.
- Sources: Google Flights (broad coverage) + direct Ryanair API.
- Normalizes results across sources.
- Batch + caching built-in.
- **Recommendation**: Test this first as a drop-in replacement or parallel provider. It may solve the empty return-leg problem we saw with pure `ryanair-py`.

**2. Flyan (koteshyelamati/Flyan)**
- New unofficial Ryanair SDK (2026) with MCP server support (great for agentic use).
- Features: `find_flights`, `cheapest_per_day`, `explore_destinations`, `find_anywhere_under`.
- Good for broad category searches ("where can I fly from BUD under €150").

**3. ryantrak (thomasdstewart/ryantrak)**
- Selenium-based round-trip scraper specifically designed for daily price tracking + CSV logging.
- Runs in GitHub Actions with debug artifacts.
- Explicitly handles return dates.

**4. Direct Ryanair Booking API (from recent X discussions)**
- Endpoint: `https://www.ryanair.com/api/booking/v4/en-gb/availability`
- Supports `TripType: "ROUNDTRIP"` + `DateOut` + `DateIn` parameters.
- More reliable than some wrapper libraries for exact round-trips.
- Can be used to patch or replace the current `ryanair-py` calls.

**5. Apify Actors (confirmed production-ready)**
- `epctex/ryanair-scraper` and `maximedupre/ryanair-scraper`: Explicit `ROUND` mode with departure + return dates.
- `makework36/flight-price-scraper`: 7-source comparison (includes both Ryanair + Wizz Air) — already noted in previous section but now confirmed as the best multi-source option.

**6. Wizz Air updates**
- `kovacskokokornel/wizzair-scraper` timetable endpoint (`/Api/search/timetable`) remains the most efficient for bulk price collection.
- parse.bot marketplace documents real endpoints (`search_flights`, `get_timetable`, `fare_finder_search` with `'anywhere'`).

### Updated Recommendation After This Round

| Priority | Tool/Library                  | Type          | Roundtrip Support | Cost     | Recommendation for Project                  |
|----------|-------------------------------|---------------|-------------------|----------|---------------------------------------------|
| 1        | farepy                        | Local Python  | Native            | Free     | Test immediately as primary Ryanair provider |
| 2        | Direct Ryanair booking API    | HTTP          | Excellent         | Free     | Implement as fallback in RyanairProvider    |
| 3        | Apify multi-source            | Hosted        | Excellent         | Low      | Use for connections + Wizz fallback         |
| 4        | Flyan                         | Local + MCP   | Good              | Free     | Good for destination exploration            |
| 5        | ryantrak (Selenium)           | Browser       | Good              | Free     | Backup for when API changes                 |

**Action**: Prioritize adding `farepy` and the direct booking API endpoint before defaulting to Apify for round-trips.

## 13. Ground Transport for Connections (Option A Research - 2026)

**Problem**: Flight searches ignore realistic ground time between airports/hubs and destinations. This is critical for 1-stop connections via VIE/MUC/FRA etc. from BUD.

**Key Findings**:
- Commercial tools (Google Flights, Skyscanner, Kayak): Use only airport MCTs. Ground between airports or to city is missing or manual.
- Kiwi.com: Best partial support; added some multimodal ground.
- Best free data sources:
  - **OSRM (OpenStreetMap)**: Free public or self-host. Table/Route API for driving time + distance. Perfect baseline.
  - **Transitous + MOTIS**: Free pan-European public transit router (GTFS + OSM). Returns real journeys with times, modes, transfers.
  - Haversine (lat/lon): Instant rough distance.
  - Rome2Rio: Good multimodal but API degraded (scrape or Apify as fallback).
  - GTFS feeds (FlixBus, national) for schedules.
- Precompute for small set (58 airports) is highly efficient.
- Academic papers used Google Maps for European airport access/egress; we can replicate with free OSRM.

**Integration Strategy (Option A)**:
- Add `GroundTransport` module.
- Enrich connections with ground legs.
- Compute total door-to-door time.
- Options in CLI and filters.
- Sources: OSRM public + Transitous public + static precompute.
- No paid keys required initially.

**Implementation Notes**:
- Use existing lat/lon in destinations.json.
- Cache ground results (stable data).
- For Apify connections: post-process with ground enrichment.
- Limitations: Public APIs have rate limits → cache + fallbacks.

**Sources**: OSRM docs, Transitous API, MOTIS, GTFS open data portals, academic papers on airport access times.

## 15. Migration Decision: farfnd/v4 + ryanair-py patterns (July 2026)

**Decision made (user: "just decide it for me and plan and implement")**:
- **Fold in**: farfnd/v4/roundTripFares as primary for round-trips (stable per 2025+ research, no PerimeterX issues like availability).
- **Fold in**: Structure and logic patterns from ryanair-py (get_cheapest_return_flights style: total price, outbound/inbound) and @2bad/ryanair (client-version refresh kept for other endpoints).
- **Deprecated**: Direct /booking/v4/availability (brittle, caused 409s; kept only as fallback).
- **Not primary**: farepy (not reliably available on PyPI in our env), Apify (optional for production scale), Flyan/ryantrak (reference only).
- **Why this**:
  - farfnd gives real EUR roundtrip prices quickly.
  - Matches community usage (travel_helper, farepy notes).
  - Enables short-stay searches for islands/seaside from BUD without one-way hacks.
  - Preserves our enforced numbered-list + emoji + links output.
- **Next for Wizz**: Similar farfnd-style or timetable endpoint.
- **Updated**: Provider now RyanairDirectProvider uses farfnd. Orchestrator prefers it for return_date_from/to.
- **Tested**: Real prices for BUD-CTA/CFU July 2026 short returns (e.g. ~€118 CTA, ~€204 CFU).

**Sources from fresh searches (web + X)**: cohaolain/ryanair-py README, @2bad/ryanair docs, sahibammar/travel_helper, farepy changelog, recent Apify actor on X.

**Implementation**: See provider rewrite, orchestrator patch, test updates. Git versioned.
