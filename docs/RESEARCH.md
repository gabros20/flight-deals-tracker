# Flight Deals Tracker - Initial Research Document

**Project**: CLI tool + skills for tracking Ryanair & Wizz Air flight prices across broad European destination categories (European islands, seaside cities, Italian less-known gems, shopping destinations, etc.). Supports deterministic searches, price history logging, change detection, Telegram notifications via cron, comparison, and best-deal discovery.

**Date**: 2026-06-19
**Location**: ~/Documents/flight-deals-tracker (git repo)
**Status**: Research complete; ready for Design + Plan docs + initial implementation.

## 1. Core Requirements from User
- Broad semantic search: "European islands", "seaside cities", "Italian hidden gems", "best shopping nearby", etc.
- Pre-loaded metadata of all Ryanair + Wizz Air destinations (IATA, city, country, coords, tags/categories, reachable from user bases).
- Deterministic scraping/search by date range, travel time/distance, price.
- Access Ryanair + Wizz prices (one-way/return, cheapest per day, availability).
- Compare across airlines/routes/dates.
- Log price history + detect changes.
- Cron jobs for periodic scans + Telegram alerts when best time to buy (price drops, deals below threshold).
- CLI tool + Hermes skill for easy use ("just tell me what you want").
- Store results locally.

## 2. Ryanair API & Scrapers (Best Options)
Ryanair has excellent public/unofficial API access (no key required for most endpoints). Multiple high-quality open-source clients:

- **@2bad/ryanair** (TypeScript, recommended reference): 
  - airports.getActive(), getClosest(), getDestinations()
  - fares.getCheapestPerDay(from, to, startDate), findDailyFaresInRange(), findCheapestRoundTrip()
  - flights.getDates(), getAvailable() (booking/availability endpoint)
  - Handles cookies, client-version for booking API.
  - ~30 stars, actively useful. Endpoints: api.ryanair.com, services-api.ryanair.com/farfnd, desktopapps.ryanair.com

- **ryanair-py** (Python by cohaolain, pypi): 
  - Ryanair(currency="EUR").get_cheapest_flights(origin, date_from, date_to)
  - get_cheapest_return_flights()
  - Simple, direct API calls. Perfect base for CLI.

- **ryanair_timecapsule** (Python): 
  - Fare-Finder + Booking API reverse-engineered.
  - Scripts for bulk download (download_fares_data.py, download_booking.py) with date ranges, duration filters.
  - Ideal for data collection / history.

- Other: ryantrak (Selenium + GitHub Actions daily tracker, CSV logging), jobezic/ryanair_scraper, AmbrazeviciusDeividas/ryanair (Telegram notifier, Lambda).

**Recommendation**: Start with Python (ryanair-py + custom extensions or port key logic from 2bad). Use for deterministic cheapest-per-day scans across many routes. Avoid Selenium where possible (use direct HTTP).

**Endpoints summary** (from gists + repos):
- Airports: https://api.ryanair.com/aggregate/3/common?...embedded=airports...
- Fares: https://api.ryanair.com/farefinder/3/oneWayFares/.../cheapestPerDay , farfnd/3/oneWayFares , roundTripFares
- Availability: /api/booking/v4/*/availability (needs correlation cookie + client-version)
- Timetables/schedules available.

Ryanair network: ~230+ destinations (Italy 32, France 25, Spain 24 top). Full lists on Wikipedia + airportoverview.com.

## 3. Wizz Air API & Scrapers
Fewer polished clients; more reverse-engineering needed. API changes versions frequently.

- **kovacskokokornel/wizzair-scraper** (Python, 16 stars): 
  - Two approaches: Timetable (fast, bulk ~80 records/call via POST https://be.wizzair.com/10.1.0/Api/search/timetable) and individual flight.
  - Supports regular + WDC (Wizz Discount Club) prices.
  - Headers critical (user-agent, origin, referer). Good starting point for timetable bulk scans.

- **projectivemotion/wizzair-scraper**, alexdevmotion/wizz-puppeteer-scraper, joshu7Su/wizzScrap, guramiivanidze/WizzairBestTrip (pandas/BS4).

- **parse.bot marketplace**: Commercial Wizz Air API wrapper (search_flights, get_flight_price_calendar, fare_finder_search with 'anywhere', get_timetable, get_all_airports, get_destinations_from_origin). Useful reference for endpoints/payloads.

- **Apify actors**: makework36/flight-price-scraper (multi-source: Ryanair + Wizz + Google Flights + others, deduped bestPrice), flight-prices-europe. HTTP-based, production-grade but paid runs.

- Old Java (wizzy) and others note frequent API version bumps (update base URL).

**Recommendation**: Implement custom Python requests client modeled on kovacskokokornel (timetable for speed) + individual for details. Handle WDC flag, bundles (BASIC/MIDDLE/PLUS). Support 'anywhere' style searches. Rate-limit carefully. Fallback to Apify actor if needed for reliability.

Wizz network: ~140-150 destinations (strong in Eastern/Central Europe, UK, Italy, Spain, Greece islands, etc.).

## 4. Combined / Multi-Source Options
- **Apify flight-price-scraper** (makework36): Pulls Ryanair, Wizz, EasyJet, Norwegian, Google Flights, Kiwi, Travelpayouts in parallel. Returns merged bestPrice + per-source map + booking links. Excellent for comparison. HTTP (no browser). Consider for initial broad scans or as skill dependency.
- **farepy** (Python): Normalized multi-source (Google Flights + Ryanair).
- **flight-finder** (self-hosted, LLM-friendly): Price history/trends, charts.

For personal deterministic use: Prefer direct Ryanair/Wizz clients + custom orchestration over paid actors long-term.

## 5. Destinations Metadata & Categorization
Need comprehensive local file (data/destinations.json or SQLite) with:
- IATA, city, country, airport_name, lat/lon (for haversine distance/travel time estimates), ryanair_base?, wizz_base?, tags/categories[], min_flight_time_est, notes.

**Sources for lists**:
- Ryanair: Wikipedia "List of Ryanair destinations", airportoverview.com/airlines/FR (~227 dests), Ryanair API itself (airports endpoint).
- Wizz: Wikipedia "List of Wizz Air destinations", airportoverview.com/airlines/W6 (~140), FlightConnections maps.
- Overlap many (e.g., ALC, BCN, BGY, BUD, CRL, etc.).

**Category ideas** (curate manually + tags):
- **European islands**: Greek (CFU, HER, JMK, JTR, RHO, SKG, ZTH, AOK), Canary (TFS, LPA, ACE), Balearic (PMI, IBZ, ALC), Malta (MLA), Cyprus (LCA, PFO), Sicily (CTA, PMO), Sardinia (CAG, AHO, OLB), Corsica (BIA, AJA), Azores/Madeira (FNC, PDL?), Irish islands, etc.
- **Seaside cities / beaches**: ALC, AGP, FAO, LIS/OPO, BCN, VCE, NAP, BDS, CTA, HER, etc.
- **Italian less-known gems**: Puglia (BRI, BDS), Calabria (SUF, REG), Sicily secondary (CTA), Sardinia, smaller like TRN/VRN, AOI, etc. Avoid mega (FCO, MXP).
- **Shopping / big cities**: LON (STN/LGW/LTN), PAR (BVA), MIL (BGY), BCN, MAD, BER, AMS, etc.
- Other: "nearby" (filter by distance from user home bases like BUD or wherever), "weekend getaways", "nature", "history", "cheap wine/food".

**Implementation**: Script to fetch/merge Ryanair/Wizz active airports → enrich with coords (from API or geonames), manual tag assignment in JSON. Support queries like "all islands reachable from STN under 3h flight or €100".

Haversine or simple great-circle for "travel time" proxy + real flight duration from API.

## 6. Tool Architecture & Components (Proposed)
- **Language/CLI**: Python 3.11+ (Typer or Click for CLI, rich for tables). Hermes skill wrapper (hermes skills).
- **Data layer**:
  - data/destinations.json (or SQLite): metadata + tags.
  - data/price_history/ or DB (timestamp, route, date, price, currency, source, url).
  - logs/ for scrapes.
- **Modules** (src/):
  - ryanair_client.py (wrap ryanair-py or direct)
  - wizz_client.py (custom requests + timetable)
  - destinations.py (load/filter by category, distance, etc.)
  - searcher.py (broad category search → generate candidate routes → cheapest scans)
  - comparator.py (best price across dates/airlines)
  - tracker.py (history, delta detection, alerts)
  - telegram_notifier.py (bot token, send_message with deals)
- **Features**:
  - `flight-deals search --category "european-islands" --from BUD --max-price 150 --dates 2026-07 --return`
  - `flight-deals track --route STN-BGY --cron "0 9 * * *"`
  - Price drop detection (>15% or absolute threshold) → Telegram.
  - Export CSV/JSON, plots (matplotlib or pandas).
- **Cron/Tracking**: APScheduler or system cron + script. GitHub Actions example from ryantrak. Persistent history.
- **Notifications**: Telegram (user's connected platform) with best deals, "buy now" signals.
- **Dependencies**: requests, typer, pandas, python-telegram-bot or httpx, haversine, pydantic. Optional: playwright for stubborn Wizz.
- **Rate limiting / robustness**: Sleeps, retries, user-agent rotation, respect ToS (personal use only). Cache results.
- **Fallbacks**: Apify actor for hard queries.

## 7. Risks, Limitations, Mitigations
- **API breakage**: Ryanair/Wizz change endpoints/versions often (esp. Wizz). Mitigation: Versioned clients, monitoring, fallback to browser/Apify.
- **Rate limits / blocks**: Aggressive scanning → IP ban. Mitigation: Slow deterministic scans, proxies (residential if needed), daily batch not real-time.
- **Legal/ToS**: Scraping public data for personal use generally OK but against airline ToS in some cases. No commercial resale. Disclaimer in tool.
- **Accuracy**: Prices dynamic; always verify on airline site. Include direct booking links.
- **Coverage**: Only Ryanair + Wizz (LCC focus). Add EasyJet later if wanted.
- **User homes**: Support multiple origins (e.g., BUD, STN, whatever user flies from). "Nearby" relative to them.
- **Data volume**: Precompute possible routes? Or on-demand with caching.

## 8. Sources & References (All Searched)
- Ryanair clients: github.com/2BAD/ryanair (primary), cohaolain/ryanair-py, mbalos16/ryanair_timecapsule, ryantrak, etc.
- Wizz: kovacskokokornel/wizzair-scraper (timetable key), parse.bot Wizz API, Apify makework36/flight-price-scraper, projectivemotion/wizzair-scraper.
- Destinations: Wikipedia Ryanair/Wizz lists, airportoverview.com (FR/W6), FlightConnections.
- Broader: Apify actors, farepy, flight-finder (self-hosted trends), ryanair-py examples.
- Categories inspiration: GetYourGuide islands, hidden gems lists (Alberobello, Meteora, etc.), Italian islands Wikipedia.

## 9. Next Steps (Immediate)
1. Write DESIGN.md (architecture, data schemas, class outlines).
2. Write PLAN.md (phased build: v0.1 destinations loader + Ryanair client, v0.2 Wizz, v0.3 tracker/cron/Telegram, v0.4 skill integration).
3. Prototype: Fetch Ryanair airports, build destinations.json skeleton with sample tags.
4. Git commits after each doc + milestone.
5. Test key endpoints locally (terminal).
This research used exhaustive web_search + GitHub discovery. All promising components identified. Tool is feasible with direct APIs for Ryanair + targeted scraping for Wizz. Ready to build.
**Repo state**: Initialized, structure created (src/data/docs/scripts). This doc committed next.

## 10. Second Round Research (X/Twitter + 2025/2026 Updates)

Additional research performed using X search and targeted web queries for recent discussions, API stability, and practical implementations (as of mid-2026).

### Ryanair Updates from X
- Confirmed: Ryanair has **no official public developer API**. All access is via reverse-engineered endpoints.
- `@2bad/ryanair` (npm) remains the most referenced unofficial wrapper in developer conversations.
- New maintained Apify actor (June 2026): `apify.com/maximedupre/ryanair-scraper` — dedicated Ryanair scraper as a low-maintenance option.
- Price tracking + Telegram alert bots using cron/polling are common personal projects.

### Wizz Air Updates from X
- No official API. Reverse-engineered endpoints (timetable + search) still functional but require active maintenance.
- API version in URL/path changes frequently (examples: 10.1.0, 24.6.0, 27.13.0 reported in 2025).
- 2024 Hungarian dev thread showed ongoing debugging of `be.wizzair.com/{version}/Api/search/timetable` and `/search` endpoints; updating version string often resolves 404s.
- Wizz deploys aggressive protections (DataDome captcha on some search endpoints, Akamai Bot Manager).
- Apify-style actors recommended for stability when self-maintenance becomes burdensome.

### Practical Telegram + Cron Price Alert Bots (from X)
- Multiple detailed threads (e.g., @micascapino_, @alp0x01) describe building working bots:
  - Poll airline calendar/pricing endpoints frequently (~every 2 minutes in one successful case).
  - Store price history in SQLite/Supabase/JSON.
  - Send instant Telegram notifications on drops below threshold.
  - Built with Node.js (`node-telegram-bot-api`) or Python, often generated via Cursor/Claude.
  - Existing public bots mentioned: AviaTipsBot, Airtrack Bot.
- Self-hosted stack that works well: Python/Node + APScheduler or GitHub Actions + SQLite + Telegram.
- Emphasis on reverse-engineering via browser DevTools Network tab (capturing exact XHR/JSON calls + headers).

### 2025/2026 Technical Challenges (Web Sources)
- **Wizz Air**: Recent breakage reports (uBlockOrigin issues, 2025) show DataDome captcha on `be.wizzair.com/{version}/Api/search/search`. Solutions involve custom filters or Playwright stealth.
- **Ryanair**: `client-version` header on booking/availability endpoint still critical (409 "Availability declined" on mismatch). Fare-finder endpoints (`/farfnd/v4/`) are more stable. Dynamic version refresh on 409 errors is the recommended pattern.
- Legal note: Ryanair has pursued legal action against scrapers (injunctions); personal/non-commercial use is lower risk.
- `2BAD/ryanair` package remains actively maintained (recent MCP tools, v3 airport endpoints, changelog updates).

### Updated Recommendations
- **Ryanair**: Prefer `@2bad/ryanair` logic or `ryanair-py` + Apify `maximedupre/ryanair-scraper` as fallback.
- **Wizz Air**: Start with dynamic-version requests client (modeled on kovacskokokornel) but have Apify or Playwright fallback ready. Monitor version strings from site bundles.
- **Bot/Tracking Layer**: Strongly consider the proven Node.js + Telegram + polling patterns shared on X for the notification/cron features. These are battle-tested for exactly this use case.
- **Overall Architecture**: Hybrid (direct API where possible + platform actor fallback + custom bot logic) gives the best resilience.

These findings reinforce the original plan while adding concrete, recent implementation patterns and warnings about API volatility.

**Repo state**: Initialized, structure created (src/data/docs/scripts). This doc committed next.