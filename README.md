# Flight Deals Tracker

Track Ryanair and Wizz Air flight deals by broad categories such as:
- European islands
- Seaside cities
- Italian hidden gems
- Shopping destinations

## Key Features (v0.4.0)

- **Category-based search** across 28+ destinations with **reachability filtering**
- **Parallel searching** + **file-based caching** (TTL, much faster repeats)
- Route tracking with **real price drop alerts** (Telegram)
- Price history logging and viewing (`history`)
- Round-trip pairing
- Destination listing by tag
- Full **configuration system**
- Cache management commands
- Proper Hermes skill for natural language use
- Hermes-managed cron jobs (via `cronjob` tool) for daily tracking and broad searches

## Installation

```bash
cd ~/Documents/flight-deals-tracker
pip install -e .
```

## Configuration

```bash
# View current config
flight-deals config

# Set defaults
flight-deals config --set-default-origin BUD

# Set up Telegram alerts (get token from @BotFather, chat_id from userinfobot)
flight-deals config --set-telegram-token 123456:ABC... --set-telegram-chat 7424678726
```

Sample config is available at `data/config.example.json`. Copy to `~/.config/flight-deals/config.json` for user-level settings.

## Main Commands

```bash
# Search with reachability + cache
flight-deals search --category european-islands --from BUD --date-from 2026-08-01 --date-to 2026-08-10 --max-price 120
# With 1-stop connections:
flight-deals search --category italian-gems --connections

# Track with alerts
flight-deals track --origin BUD --destination CAG --date-out 2026-08-12 --threshold 10

# View history
flight-deals history --origin BUD --destination CFU

# Cache management
flight-deals cache stats
flight-deals cache list
flight-deals cache clear
```

## Scheduled Tracking (managed by Hermes)

Crons are set up using Hermes' own `cronjob` system (as per Hermes SOP for assistant tasks). Hermes will manage and run the tracking autonomously.

Jobs currently configured:
- Daily at 9am: Run tracking script + summarize price changes/alerts (delivered here)
- Mondays at 10am: Broad search for new deals across categories

To manage:
- `hermes cron list`
- `hermes cron pause <id>` etc.

You can still run manually: `python scripts/daily_track.py`

The daily_track.py script is the execution engine that Hermes calls.

## Hermes Skill

Installed at `~/.hermes/skills/travel/flight-deals/`

Ask Hermes:
- "Find me the cheapest European island deals from Budapest next month under 150 euros"
- "Track BUD to CAG on August 12th and alert on any drop over 10%"
- "Show cache stats" or "clear the flight cache"

## Notes

- Always verify final prices on the official airline websites.
- Caching and reachability dramatically reduce API calls.
- The project is fully versioned with git.

## Apify Integration (Connections & Multi-Airline)
The tool now supports Apify as the cheapest way to get true connection and multi-airline results.

- Use `--connections` flag to enable.
- Requires free Apify account + token (set via env `APIFY_TOKEN` or config).
- Cost: ~$0.0003 per search (heavily cached).
- Results include stops count and source (Google Flights / Kiwi / etc.).
- Virtual interlining / self-transfer support.

Example:
```
flight-deals search --category european-islands --date-from 2026-08-01 --date-to 2026-08-10 --connections
```

See `data/config.example.json` and docs/ for setup.

## Ground Transport & Efficient Connections (Phase 7 Additions)
The tool now accounts for realistic ground time and options between airports when using `--connections`.

**New CLI flags for search:**
- `--max-ground-minutes 120` — Filter out connections with excessive ground time.
- `--ground-prefer public` — Prefer train/bus over driving (driving|public|any).
- `--sort-by efficiency` — Sort by price-per-total-hour or total-time.

**Features:**
- Ground legs only applied for reasonable distances (<400km).
- Precomputed data for common BUD hub pairs (instant, offline).
- Efficiency scoring (€ / total door-to-door hour).
- Uses OSRM (driving) + Transitous (public transit) + haversine fallback.
- Total time = air time + ground + buffer.

Example:
`flight-deals search --category seaside --connections --max-ground-minutes 90 --ground-prefer public --sort-by total-time`

See docs/ for full research and design.


## Multi-Airport Self-Transfer Hubs (Phase 8)

The tool now supports realistic connections via cities with multiple airports:

- Istanbul (IST/SAW)
- Milan (BGY/MXP)
- London (STN/LGW/LTN)
- Rome (CIA/FCO)
- Paris (BVA/CDG)
- Brussels (CRL/BRU)
- Warsaw (WAW/WMI)

Use `--connections` to include self-transfer options with ground transport time between airports in the same city.

Example:
```
flight-deals search --category european-islands --connections --max-ground-minutes 120
```

New command:
```
flight-deals multi-airports
```

Ground times are calculated using OSRM + public transit data and precomputed for speed.
