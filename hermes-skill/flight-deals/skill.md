---
name: flight-deals
description: "Search and track Ryanair & Wizz Air flight deals by semantic categories."
version: 0.3.0
author: User
tags: [flights, ryanair, wizzair]
---

See wrapper.py for Hermes integration functions.

This is the in-repo version of the skill. The canonical installed version lives at:
~/.hermes/skills/travel/flight-deals/
## New in v0.5: History Comparisons
- Use `collect` to build your price history database.
- Searches now return badges and notes comparing to your historical data for the route.
- Commands: `collect`, `history-stats`.

## New in v0.6.0: Optimizations
- Robust date-window filtering via --history-window or config.
- Pure file-based CSV storage (git committed, with cache).
- Cron-ready collection script + alerts on drops below historical avg.
- `flight-deals alerts`, `history-stats --window`, auto-alerts in collect.
- All data stays in data/*.csv files.

Hermes cron example (add via Hermes CLI):
Use cronjob to run the collect script periodically and deliver alerts to Telegram.

