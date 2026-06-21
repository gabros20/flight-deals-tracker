---
name: flight-deals
description: Search and track Ryanair & Wizz Air deals by category
---

## Capabilities
- Search flights by semantic categories (european-islands, seaside, italian-gems, shopping)
- Track specific routes with price drop alerts
- List destinations by tag
- Support one-way and return trips

## Usage Examples (via Hermes)
- "Find European island deals from Budapest in August under 150 euros"
- "Track STN to BGY on August 20th with 15% threshold"
- "Show me seaside destinations"

## Functions
- `search_deals(category, origin, date_from, date_to, max_price)`
- `track_route(origin, destination, date_out, threshold)`
- `list_destinations(tag)`

See `wrapper.py` for the implementation.