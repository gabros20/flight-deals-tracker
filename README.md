# Flight Deals Tracker

Track Ryanair and Wizz Air flight deals by broad categories such as:
- European islands
- Seaside cities
- Italian hidden gems
- Shopping destinations

## Features
- Category-based search across 28+ destinations (`search`)
- Parallel searching for speed
- Route tracking with price drop alerts (`track`)
- Price history logging and viewing (`history`)
- Round-trip pairing
- Destination listing by tag (`destinations`)
- Proper Hermes skill (installed at `~/.hermes/skills/travel/flight-deals/`)
- Natural language usage via Hermes agent

## Installation

```bash
cd ~/Documents/flight-deals-tracker
pip install -e .
```

## Hermes Skill

A proper Hermes skill has been created at:

`~/.hermes/skills/travel/flight-deals/`

You can now ask Hermes things like:
- "Find cheap European island flights from Budapest in August under 150 euros"
- "Track BUD to CAG on July 25th"

## Data

Curated list of Ryanair + Wizz destinations with rich tagging.

## Notes

- Always double-check prices on the official airline sites.
- The project is versioned with git.