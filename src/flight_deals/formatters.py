"""
Consistent result formatting for CLI and cron reports.
Enforces the emoji + link format.
"""

from typing import Dict, Any, List

def format_deal(deal: Dict[str, Any], index: int) -> str:
    """
    Format a single deal in the required emoji + link format.
    Example:
    1. BUD → CTA €185.00  2026-07-08 → 2026-07-12
       ✈️ Google Flights: https://www.google.com/travel/flights?q=...
       📍 Maps: https://www.google.com/maps?q=...
       🏞️ Images: https://images.google.com/search?q=...
    """
    origin = deal.get("origin", "BUD")
    dest = deal.get("destination", "CTA")
    price = deal.get("price", 0)
    currency = deal.get("currency", "EUR")
    dep = deal.get("outbound_date", "")
    ret = deal.get("return_date", "")
    source = deal.get("source", "")

    query = f"{origin} to {dest} on {dep} return {ret}"

    flights_url = f"https://www.google.com/travel/flights?q={query.replace(' ', '+')}"
    maps_url = f"https://www.google.com/maps?q={dest}+airport"
    images_url = f"https://images.google.com/search?q={query.replace(' ', '+')}+seaside"

    lines = [
        f"{index}. {origin} → {dest} {currency}{price:.2f}  {dep} → {ret}",
        f"   ✈️ [Google Flights]({flights_url})",
        f"   📍 [Maps]({maps_url})",
        f"   🏞️ [Images]({images_url})",
    ]
    if source:
        lines.append(f"   Source: {source}")
    if deal.get("notes"):
        notes = deal["notes"]
        lines.append(f"   {notes}")
    return "\n".join(lines)


def format_results(results: List[Dict[str, Any]], title: str = "Flight Deals") -> str:
    """Format a list of deals for CLI or cron reports."""
    if not results:
        return f"{title}\n\nNo good deals found."

    header = f"{title}\n\n"
    body = "\n\n".join(format_deal(deal, i+1) for i, deal in enumerate(results))
    return header + body
