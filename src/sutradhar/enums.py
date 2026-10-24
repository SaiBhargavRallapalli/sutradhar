"""Shared enumerations used across the domain model and the planning engines.

Every enum is a ``str`` subclass so values serialise to JSON/CSV naturally and
can be compared against user input case-insensitively via :meth:`parse`.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CategoryKind",
    "CityTier",
    "EventType",
    "OwnerRole",
    "Priority",
    "RiskLevel",
    "Season",
    "Source",
    "TaskStatus",
    "WindExposure",
]


class _Parsable(Enum):
    """Mixin adding a forgiving, case-insensitive string parser."""

    @classmethod
    def parse(cls, value: str) -> _Parsable:
        """Parse ``value`` into a member, accepting names *or* values.

        Accepts ``"wedding"``, ``"WEDDING"`` and ``"EventType.WEDDING"``.
        Raises ``ValueError`` listing every legal option when it cannot match.
        """
        raw = value.strip().lower()
        for member in cls:
            if raw in {
                member.value.lower(),
                member.name.lower(),
                f"{cls.__name__}.{member.name}".lower(),
            }:
                return member
        options = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown {cls.__name__} {value!r}; expected one of: {options}")


class EventType(str, _Parsable):
    """The celebration vertical an event belongs to."""

    WEDDING = "wedding"
    BIRTHDAY = "birthday"
    COMMUNITY = "community"
    HOUSEWARMING = "housewarming"
    ANNIVERSARY = "anniversary"


class CityTier(str, _Parsable):
    """Rough cost-of-living band for the host city.

    ``GLOBAL`` models an international/metro-Abroad budget where venue and
    labour prices are typically well above an Indian tier-1 metro.
    """

    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3 = "tier_3"
    GLOBAL = "global"


class CategoryKind(str, _Parsable):
    """How a budget category behaves as guest count changes.

    ``FIXED``      - cost is independent of guest count (e.g. photography package).
    ``PER_GUEST``  - cost is strictly proportional to guest count (e.g. catering).
    ``SCALING``    - grows with guests but with an economy of scale (e.g. decor).
    """

    FIXED = "fixed"
    PER_GUEST = "per_guest"
    SCALING = "scaling"


class Priority(str, _Parsable):
    """How much the host cares about a category or vendor."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    VITAL = "vital"

    @property
    def weight(self) -> float:
        """Multiplier applied to a category's share of the budget."""
        return {
            Priority.LOW: 0.7,
            Priority.NORMAL: 1.0,
            Priority.HIGH: 1.3,
            Priority.VITAL: 1.6,
        }[self]


class TaskStatus(str, _Parsable):
    """Progress of a single timeline task."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class RiskLevel(str, _Parsable):
    """Ordinal severity band for risks and trade-off impacts."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3, RiskLevel.CRITICAL: 4}[
            self
        ]


class Source(str, _Parsable):
    """Provenance of a plan section - engine-computed, catalogue, or model-written?"""

    ENGINE = "engine"
    CATALOG = "catalogue"
    MODEL = "model"
    HYBRID = "hybrid"


class OwnerRole(str, _Parsable):
    """Who is realistically responsible for a task, vendor or risk."""

    HOST = "host"
    FAMILY = "family"
    PLANNER = "planner"
    VENDOR = "vendor"
    VOLUNTEER = "volunteer"
    FINANCE = "finance"
    LOGISTICS = "logistics"
    GUESTS = "guests"


class Season(str, _Parsable):
    """Demand season for the event's category, driving price and lead-time advice."""

    PEAK = "peak"
    SHOULDER = "shoulder"
    OFF_PEAK = "off_peak"


class WindExposure(str, _Parsable):
    """Outdoor exposure - feeds candle/paper-decoration risk guidance."""

    INDOOR = "indoor"
    SHELTERED = "sheltered"
    OPEN_AIR = "open_air"
