from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class Airport(BaseModel):
    iata: str = Field(..., min_length=3, max_length=3)
    city: str
    country: str
    lat: float
    lon: float
    tags: list[str] = Field(default_factory=list)
    is_ryanair_base: bool = False
    is_wizz_base: bool = False


class FlightDeal(BaseModel):
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    price: float
    currency: str
    source: str  # "ryanair", "wizz", "apify:google_flights", "apify:kiwi" etc.
    flight_number: Optional[str] = None
    duration_minutes: Optional[int] = None
    # New fields for connections / multi-source
    stops: int = 0
    source_details: Dict[str, Any] = Field(default_factory=dict)
    booking_url: Optional[str] = None
    # Ground transport enrichment for connections
    ground_leg: Optional["GroundLeg"] = None
    total_duration_minutes: Optional[int] = None
    efficiency_score: Optional[float] = None
    # Phase 8: Full connection path support
    connection_path: List[Dict[str, Any]] = Field(default_factory=list)  # e.g. [{"type": "flight", "from": "BUD", "to": "BGY", ...}, {"type": "ground", ...}, ...]
    notes: str = ""



class PriceSnapshot(BaseModel):
    timestamp_utc: datetime
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    price: float
    currency: str
    source: str


class GroundLeg(BaseModel):
    """Represents a ground transport leg between two airports or airport and destination."""
    from_iata: str
    to_iata: str
    mode: str  # "driving", "train", "bus", "taxi", "public_transit"
    duration_minutes: int
    distance_km: float
    estimated_cost_eur: Optional[float] = None
    notes: str = ""
    options: List[str] = Field(default_factory=list)  # e.g. ["train", "bus"] for multi