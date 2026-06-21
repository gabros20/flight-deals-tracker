# Flight Deals Tracker

Track Ryanair and Wizz Air flight deals by broad categories such as:
- European islands
- Seaside cities
- Italian hidden gems
- Shopping destinations

## Features
- Category-based search (`search`)
- Route tracking with price drop alerts (`track`)
- Destination listing by tag (`destinations`)
- Price history logging
- Telegram notifications
- Hermes skill wrapper for natural language use

## Usage Examples

```bash
# Search for European island deals from Budapest
flight-deals search --category european-islands --from BUD --date-from 2026-08-01 --date-to 2026-08-10 --max-price 150

# Track a specific route with price drop alerts
flight-deals track --origin STN --destination BGY --date-out 2026-08-20 --threshold 15

# List destinations tagged as "seaside"
flight-deals destinations --tag seaside
```

## Installation

```bash
pip install -e .
```

## Hermes Skill

The tool can be used via natural language through the Hermes agent using the wrapper in `hermes-skill/flight-deals/`.

See `docs/PLAN.md` for the full development roadmap.