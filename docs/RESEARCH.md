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
