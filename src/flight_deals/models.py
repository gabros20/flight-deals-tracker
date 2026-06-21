from datetime import datetime
from typing import Optional
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
    source: str  # "ryanair" or "wizz"
    flight_number: Optional[str] = None
    duration_minutes: Optional[int] = None


class PriceSnapshot(BaseModel):
    timestamp_utc: datetime
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str] = None
    price: float
    currency: str
    source: str