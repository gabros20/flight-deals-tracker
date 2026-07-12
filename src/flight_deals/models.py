from typing import Literal, Union, List, Optional, Dict, Any
from datetime import datetime
from pydantic import AliasChoices, BaseModel, Field

Confidence = Literal["exact", "approximate"]


class DayFare(BaseModel):
    """
    One direction, one day, cheapest fare — the unit of day-level data
    (CAL / TT). Fields per Task 3 brief + CONTRACT.md naming.
    """
    origin: str
    destination: str
    date: str  # airport-local calendar date "YYYY-MM-DD"
    price_eur: float
    currency_original: str
    price_confidence: Confidence
    carrier: str  # "ryanair" | "wizzair"
    source_endpoint: str  # e.g. "farfnd/oneWayFares/cheapestPerDay"
    # Enrichment present on some endpoints (nullable — day-level often has none)
    departure_time: Optional[str] = None  # "HH:MM"
    flight_number: Optional[str] = None


class FareLeg(BaseModel):
    """A single priced flight leg inside a FarePair."""
    origin: str
    destination: str
    date: str  # "YYYY-MM-DD"
    price_eur: float
    carrier: str
    departure_time: Optional[str] = None  # "HH:MM"
    arrival_time: Optional[str] = None
    flight_number: Optional[str] = None
    duration_minutes: Optional[int] = None


class FarePair(BaseModel):
    """
    A paired round-trip (RT-ANYWHERE / RT-EXACT): outbound + inbound legs and a
    total. Flight numbers/times are present because farfnd roundTripFares
    returns them.
    """
    origin: str
    destination: str
    out_date: str
    return_date: str
    nights: int
    total_price_eur: float
    currency_original: str
    price_confidence: Confidence
    carrier: str
    source_endpoint: str
    outbound: FareLeg
    inbound: FareLeg


# NOTE (Task 6): the dead ``ProviderStatus`` pydantic model was removed. Per-
# provider health has exactly ONE representation across the codebase — the
# dict produced by ``orchestrator.aggregate_status`` (``{provider: {ok, status,
# calls, errors, last_error}}``), which the planner reuses and ``output.py``
# projects down to the frozen ``sources`` map (``{provider: status_string}``).
# There is no second status type to keep in sync.


class Airport(BaseModel):
    iata: str = Field(..., min_length=3, max_length=3)
    city: str
    country: str
    lat: float
    lon: float
    tags: list[str] = Field(default_factory=list)
    is_ryanair_base: bool = False
    is_wizz_base: bool = False
    # City-anchor hybrid transit refinement (Task 14): the CITY-CENTER lat/lon
    # used for the hybrid line-haul query (shared across a multi-airport city so
    # MXP and BGY both anchor on Milan), and an optional per-airport
    # airport-access pad override (minutes; default 30 in the model) for
    # notoriously-far airports (BVA, STN…). Nullable — older data without them
    # simply has no city anchor (the hybrid pass skips such pairs).
    city_lat: Optional[float] = None
    city_lon: Optional[float] = None
    access_pad_minutes: Optional[int] = None

# --------------------------------------------------------------------------- #
# Gem destinations (Task 15) — curated non-airport places reached via a gateway #
# airport + an onward ferry/bus/train chain. A terminal EXTENSION of a deal,    #
# never a new shape (see docs/SEARCH-DESIGN.md §2b).                            #
# --------------------------------------------------------------------------- #
class GemLeg(BaseModel):
    """One onward ground/water hop of a gem's gateway chain. ``from_place``/
    ``to_place`` are human place labels (e.g. "Rhodes Town port"), not IATAs —
    a gem's chain runs between ports/towns, not airports."""
    mode: str  # bus | train | taxi | ferry | shuttle
    from_place: str = Field(validation_alias=AliasChoices("from_place", "from"))
    to_place: str = Field(validation_alias=AliasChoices("to_place", "to"))
    minutes: int
    cost_eur: float
    model_config = {"populate_by_name": True, "extra": "ignore"}


class GemGateway(BaseModel):
    """One airport a gem is reachable from, plus the onward chain, its curated
    totals, an operator/frequency note, and an optional per-gateway ``season``
    window (overrides the gem-level season for this gateway)."""
    airport: str
    legs: List[GemLeg]
    total_minutes: int
    total_cost_eur: float
    note: str = ""
    season: Optional[str] = None
    model_config = {"extra": "ignore"}


class Gem(BaseModel):
    """A curated gem destination (small island etc.). ``marginal`` gems are
    excluded from default ``--where`` matching and reachable only via ``--to``
    (day-trip/awkward-connection caveats live in the gateway ``note``).
    ``season`` (gem-level) applies to gateways that don't set their own."""
    slug: str
    name: str
    country: str
    tags: List[str]
    gateways: List[GemGateway]
    season: Optional[str] = None
    marginal: bool = False
    model_config = {"extra": "ignore"}


class FlightLeg(BaseModel):
    type: Literal["flight"] = "flight"
    origin: str
    destination: str
    price: float
    duration_minutes: Optional[int] = None
    source: str
    departure_date: Optional[str] = None

class GroundLeg(BaseModel):
    """Represents a ground transport leg between two airports.

    Field naming (CONTRACT §7 open item, RESOLVED 2026-07-11): the cost field is
    ``cost_eur`` — the name CONTRACT §2 froze for ground legs and the ground
    summary. It accepts the legacy ``estimated_cost_eur`` key on input (older
    ``data/ground_transfers.json`` rows, embedded history dicts) via a validation
    alias, so nothing that wrote the old key breaks; the attribute and the
    serialised key are both ``cost_eur``.
    """
    type: Literal["ground"] = "ground"
    from_iata: str
    to_iata: str
    mode: str  # "driving", "public_transit", "train", "bus"
    duration_minutes: int
    distance_km: Optional[float] = None
    cost_eur: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("cost_eur", "estimated_cost_eur"),
    )
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
