from typing import Literal, Union, List, Optional, Dict, Any
from datetime import datetime
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

class FlightLeg(BaseModel):
    type: Literal["flight"] = "flight"
    origin: str
    destination: str
    price: float
    duration_minutes: Optional[int] = None
    source: str
    departure_date: Optional[str] = None

class GroundLeg(BaseModel):
    """Represents a ground transport leg between two airports."""
    type: Literal["ground"] = "ground"
    from_iata: str
    to_iata: str
    mode: str  # "driving", "public_transit"
    duration_minutes: int
    distance_km: float
    estimated_cost_eur: Optional[float] = None
    notes: str = ""
    options: List[str] = Field(default_factory=list)

Leg = Union[FlightLeg, GroundLeg]

class HistoricalComparison(BaseModel):
    """Historical price comparison for a route/date."""
    count: int = 0
    min_price: Optional[float] = None
    avg_price: Optional[float] = None
    median_price: Optional[float] = None
    max_price: Optional[float] = None
    best_this_month: bool = False
    best_this_year: bool = False
    percentile_25: Optional[float] = None
    percentile_75: Optional[float] = None
    comparison_note: str = ""
    last_collected: Optional[str] = None

class FlightDeal(BaseModel):
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    price: float
    currency: str
    source: str  # "ryanair", "wizz", "apify:google_flights", "self-transfer:Milan" etc.
    flight_number: Optional[str] = None
    duration_minutes: Optional[int] = None
    # Connection fields
    stops: int = 0
    source_details: Dict[str, Any] = Field(default_factory=dict)
    booking_url: Optional[str] = None
    # Ground / connections
    ground_leg: Optional[GroundLeg] = None
    total_duration_minutes: Optional[int] = None
    efficiency_score: Optional[float] = None
    connection_path: List[Dict[str, Any]] = Field(default_factory=list)
    legs: List[Leg] = Field(default_factory=list)
    notes: str = ""
    # Historical price data (Phase 9)
    historical_comparison: Optional[HistoricalComparison] = None
    comparison_note: str = ""

class PriceSnapshot(BaseModel):
    timestamp_utc: datetime
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    price: float
    currency: str
    source: str
    # Optional full context for composites
    connection_path: List[Dict[str, Any]] = Field(default_factory=list)
    total_price: Optional[float] = None  # for composites

# Keep legacy GroundLeg definition for compatibility (if needed)
class GroundLegLegacy(BaseModel):
    """Legacy ground leg for backward compat."""
    from_iata: str
    to_iata: str
    mode: str
    duration_minutes: int
    distance_km: float
    estimated_cost_eur: Optional[float] = None
    notes: str = ""
    options: List[str] = Field(default_factory=list)
