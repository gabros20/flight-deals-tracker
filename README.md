# Flight Deals Tracker

A powerful CLI tool for discovering and tracking **Ryanair** and **Wizz Air** flight deals from Budapest (BUD) using broad, intelligent categories.

## Features

- **Category-based searches** (european-islands, seaside, italian-gems, shopping, etc.)
- **Short-stay round-trip** support with flexible date windows
- **1-stop connections** with multi-airport ground transport logic (Milan, London, Istanbul, Rome, etc.)
- **Price history tracking** + automatic price drop alerts
- **Smart caching** (15-minute TTL by default) with `--fresh` bypass option
- **Telegram notifications** for deals and price drops
- **Hermes integration** (skill + cron jobs)

## Installation

```bash
cd ~/Documents/flight-deals-tracker
pip install -e .
```

## Quick Start

```bash
# Search for seaside deals (Italy & Greece focus)
flight-deals search --category seaside --from BUD --date-from 2026-07-05 --date-to 2026-07-12 --return-from 2026-07-08 --return-to 2026-07-19 --connections

# Force fresh prices (bypass cache)
flight-deals search --category italian-gems --fresh

# View price history
flight-deals history --origin BUD --destination BRI
```

## Key Commands

| Command                    | Description                              |
|---------------------------|------------------------------------------|
| `search`                  | Category-based flight search             |
| `history`                 | View price history for a route           |
| `cache stats`             | Show cache statistics                    |
| `cache clear`             | Clear all cached results                 |
| `config`                  | View or update configuration             |

## Configuration

```bash
# Set your home airport
flight-deals config --set-default-origin BUD

# Configure Telegram alerts
flight-deals config --set-telegram-token YOUR_TOKEN --set-telegram-chat YOUR_CHAT_ID
```

## Recent Improvements

- Cache TTL reduced to **15 minutes** for fresher prices
- Added `--fresh` flag to bypass cache entirely
- Improved Google Flights, Maps, and Images links

## License

MIT
