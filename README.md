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
- Cron-ready daily tracking script

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

# Track with alerts
flight-deals track --origin BUD --destination CAG --date-out 2026-08-12 --threshold 10

# View history
flight-deals history --origin BUD --destination CFU

# Cache management
flight-deals cache stats
flight-deals cache list
flight-deals cache clear
```

## Cron / Scheduled Tracking

Use the included script:

```bash
# Run manually
python scripts/daily_track.py

# Example cron (every day at 9am)
0 9 * * * cd /Users/macmini/Documents/flight-deals-tracker && /path/to/venv/bin/python scripts/daily_track.py >> ~/flight-deals-cron.log 2>&1
```

You can customize the routes inside `scripts/daily_track.py` or pass them via arguments.

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