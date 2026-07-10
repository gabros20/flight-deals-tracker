"""``SearchSpec`` — the one declarative artifact agents/cron/humans produce
(SEARCH-DESIGN §4). The planner compiles it; ``brief`` diffs it; saved searches
store it.

This module owns:

* the pydantic ``SearchSpec`` model (forward-compatible: it *accepts* all four
  trip shapes; the planner refuses the not-yet-enabled ones);
* the small date-DSL parser for ``depart`` (``YYYY-MM-DD``,
  ``YYYY-MM-DD..YYYY-MM-DD``, ``YYYY-MM``, comma lists) resolved into a concrete
  ``DepartSpec`` (always an ``out_from``/``out_to`` window plus the raw month/
  date-list detail the planner needs for TT ranges and calendars);
* the ``nights`` ``"X-Y"`` range parser;
* ``SpecError`` — every validation failure carries a ``hint`` with an exact
  corrected example, so the CLI can emit the frozen exit-2 envelope (CONTRACT §3)
  without re-deriving advice.
"""

from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# The four trip shapes the spec vocabulary accepts (SEARCH-DESIGN §2 / §4).
# Only ``direct`` executes in Task 6; the planner refuses the rest with a hint.
Shape = Literal["direct", "extended-origin", "open-jaw", "via-hub"]
Carrier = Literal["ryanair", "wizzair"]

_IATA_LEN = 3


class SpecError(ValueError):
    """A spec-validation failure. ``hint`` is an exact corrected example (never
    generic advice) so the CLI maps it straight to the exit-2 envelope."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# depart DSL                                                                   #
# --------------------------------------------------------------------------- #
class DepartSpec(BaseModel):
    """A resolved outbound-departure constraint.

    Always exposes a concrete ``[out_from, out_to]`` window (what RT-ANYWHERE
    and the TT range are built from), plus the original ``kind`` and the
    ``dates``/``month`` detail so calendar/date-list logic downstream isn't
    lossy.
    """

    kind: Literal["window", "month", "dates"]
    out_from: str  # ISO YYYY-MM-DD
    out_to: str  # ISO YYYY-MM-DD (>= out_from)
    dates: List[str] = Field(default_factory=list)
    month: Optional[str] = None  # "YYYY-MM" when kind == "month"


_DEPART_HINT = (
    'depart must be a date "2026-08-22", a window "2026-08-22..2026-08-24", '
    'a month "2026-08", or a comma list "2026-08-22,2026-08-29"'
)


def _iso(d: str) -> date:
    return date.fromisoformat(d)


def parse_depart(expr: str) -> DepartSpec:
    """Parse the ``depart`` DSL into a resolved :class:`DepartSpec`.

    Grammar (SEARCH-DESIGN §4): ``YYYY-MM-DD`` | ``YYYY-MM-DD..YYYY-MM-DD`` |
    ``YYYY-MM`` | comma list of any single dates. Whitespace tolerant.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise SpecError("depart is required", _DEPART_HINT)
    raw = expr.strip()

    # Window: A..B
    if ".." in raw:
        parts = [p.strip() for p in raw.split("..")]
        if len(parts) != 2 or not all(parts):
            raise SpecError(f"depart window {raw!r} is malformed", _DEPART_HINT)
        try:
            a, b = _iso(parts[0]), _iso(parts[1])
        except ValueError:
            raise SpecError(f"depart window {raw!r} has an unparsable date", _DEPART_HINT)
        if b < a:
            raise SpecError(
                f"depart window end {parts[1]} is before start {parts[0]}",
                f'flip them: "{parts[1]}..{parts[0]}"',
            )
        return DepartSpec(kind="window", out_from=a.isoformat(), out_to=b.isoformat())

    # Comma list of single dates
    if "," in raw:
        items = [p.strip() for p in raw.split(",") if p.strip()]
        if not items:
            raise SpecError(f"depart list {raw!r} is empty", _DEPART_HINT)
        try:
            days = sorted({_iso(x) for x in items})
        except ValueError:
            raise SpecError(f"depart list {raw!r} has an unparsable date", _DEPART_HINT)
        return DepartSpec(
            kind="dates",
            out_from=days[0].isoformat(),
            out_to=days[-1].isoformat(),
            dates=[d.isoformat() for d in days],
        )

    # Month: YYYY-MM (exactly 7 chars, no day)
    if len(raw) == 7 and raw[4] == "-":
        try:
            first = _iso(raw + "-01")
        except ValueError:
            raise SpecError(f"depart month {raw!r} is not a valid YYYY-MM", _DEPART_HINT)
        # last day of month
        if first.month == 12:
            nxt = date(first.year + 1, 1, 1)
        else:
            nxt = date(first.year, first.month + 1, 1)
        last = date.fromordinal(nxt.toordinal() - 1)
        return DepartSpec(
            kind="month",
            out_from=first.isoformat(),
            out_to=last.isoformat(),
            month=raw,
        )

    # Single date
    try:
        d = _iso(raw)
    except ValueError:
        raise SpecError(f"depart {raw!r} is not a recognised date/window/month", _DEPART_HINT)
    return DepartSpec(kind="dates", out_from=d.isoformat(), out_to=d.isoformat(), dates=[d.isoformat()])


# --------------------------------------------------------------------------- #
# nights DSL                                                                   #
# --------------------------------------------------------------------------- #
_NIGHTS_HINT = 'nights must be "N" or a range "5-8" (min-max, min <= max, both >= 0)'


def parse_nights(expr: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse ``nights`` into an inclusive ``(min, max)`` range. ``None`` means
    one-way (no return). ``"5"`` -> ``(5, 5)``; ``"5-8"`` -> ``(5, 8)``."""
    if expr is None:
        return None
    if isinstance(expr, int):
        expr = str(expr)
    raw = str(expr).strip()
    if not raw:
        return None
    try:
        if "-" in raw:
            lo_s, hi_s = raw.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(raw)
    except ValueError:
        raise SpecError(f"nights {raw!r} is not a number or range", _NIGHTS_HINT)
    if lo < 0 or hi < 0:
        raise SpecError(f"nights {raw!r} cannot be negative", _NIGHTS_HINT)
    if hi < lo:
        raise SpecError(f"nights range {raw!r} has max < min", f'try "{hi}-{lo}"')
    return (lo, hi)


# --------------------------------------------------------------------------- #
# SearchSpec                                                                   #
# --------------------------------------------------------------------------- #
class SearchSpec(BaseModel):
    """The declarative search request (SEARCH-DESIGN §4).

    Construct via :func:`parse_spec` from agent/CLI/YAML input so DSL fields
    (``depart``/``nights``) are validated with actionable hints. The raw string
    forms are kept on the model (round-trips cleanly to YAML/JSON) and the
    resolved forms are exposed as ``depart_spec`` / ``nights_range``.
    """

    origins: List[str] = Field(default_factory=lambda: ["BUD"])
    where: Optional[str] = None
    depart: str
    nights: Optional[str] = None
    shapes: List[Shape] = Field(default_factory=lambda: ["direct"])
    via: object = "none"  # "auto" | ["VIE", ...] | "none" (only used by via-hub, Task 10)
    budget: Optional[float] = None  # EUR, total per person
    carriers: List[Carrier] = Field(default_factory=lambda: ["ryanair", "wizzair"])
    max_results: int = Field(default=10, ge=1)

    model_config = {"extra": "forbid"}

    # --- normalisation ---------------------------------------------------- #
    @field_validator("origins", mode="before")
    @classmethod
    def _norm_origins(cls, v):
        if isinstance(v, str):
            v = [v]
        if not v:
            raise ValueError("origins cannot be empty")
        out = []
        for o in v:
            o = str(o).strip().upper()
            if len(o) != _IATA_LEN or not o.isalpha():
                raise ValueError(
                    f"origin {o!r} is not a 3-letter IATA code (e.g. BUD, VIE)"
                )
            out.append(o)
        return out

    @field_validator("carriers", mode="before")
    @classmethod
    def _norm_carriers(cls, v):
        if isinstance(v, str):
            v = [v]
        return [str(c).strip().lower() for c in v]

    @field_validator("shapes", mode="before")
    @classmethod
    def _norm_shapes(cls, v):
        if isinstance(v, str):
            v = [v]
        return [str(s).strip().lower() for s in v]

    @field_validator("nights", "where", mode="before")
    @classmethod
    def _stringify(cls, v):
        return None if v is None else str(v)

    @model_validator(mode="after")
    def _validate_dsl(self):
        # These raise SpecError (with hints); parse_spec surfaces them.
        self._depart_spec = parse_depart(self.depart)
        self._nights_range = parse_nights(self.nights)
        return self

    # --- resolved accessors ---------------------------------------------- #
    @property
    def depart_spec(self) -> DepartSpec:
        return self._depart_spec

    @property
    def nights_range(self) -> Optional[tuple[int, int]]:
        return self._nights_range

    @property
    def is_round_trip(self) -> bool:
        return self._nights_range is not None


def _field_hint(errors: list) -> str:
    """Turn the first pydantic error into an exact corrected example."""
    example = (
        '{"origins":["BUD"],"where":"seaside","depart":"2026-08-22..2026-08-24",'
        '"nights":"5-8","budget":180,"max_results":10}'
    )
    if errors:
        e = errors[0]
        loc = ".".join(str(x) for x in e.get("loc", ())) or "spec"
        msg = e.get("msg", "invalid value")
        return f"fix field {loc!r}: {msg}. Example spec: {example}"
    return f"Example spec: {example}"


def parse_spec(data: dict) -> SearchSpec:
    """Build a validated :class:`SearchSpec` from a raw dict (parsed JSON/YAML).

    A top-level ``spec:`` wrapper (the saved-search file layout, SEARCH-DESIGN
    §4) is unwrapped automatically so the same loader reads both a bare spec
    (inline ``--spec '{json}'``) and a saved-search file. Any validation failure
    is normalised to :class:`SpecError` with an actionable ``hint``.
    """
    if not isinstance(data, dict):
        raise SpecError("spec must be a JSON/YAML object", _field_hint([]))
    if "spec" in data and isinstance(data["spec"], dict):
        data = data["spec"]
    try:
        return SearchSpec(**data)
    except SpecError:
        raise
    except ValidationError as e:
        # A SpecError raised inside a validator arrives wrapped; unwrap it.
        for err in e.errors():
            ctx_exc = err.get("ctx", {}).get("error")
            if isinstance(ctx_exc, SpecError):
                raise ctx_exc
        raise SpecError(f"invalid spec: {e.error_count()} error(s)", _field_hint(e.errors()))
    except TypeError as e:
        raise SpecError(f"invalid spec: {e}", _field_hint([]))
