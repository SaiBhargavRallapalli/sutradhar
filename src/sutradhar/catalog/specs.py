"""Schema for the event *knowledge catalog*.

The catalog is declarative data, not code: each celebration vertical ships a
:class:`EventTypeProfile` holding budget categories, timeline tasks, vendors,
risks, themes and a day-of run sheet. Adding a new vertical (say, a Christening
or a Hackathon) means writing one new module that returns an
:class:`EventTypeProfile` - no engine changes, no interface changes. See
``docs/ALGORITHMS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from ..enums import CategoryKind, EventType, OwnerRole, Priority, RiskLevel

__all__ = [
    "CategorySpec",
    "EventTypeProfile",
    "RiskSpec",
    "RunOfShowSpec",
    "TaskSpec",
    "ThemeSpec",
    "VendorSpec",
    "risk",
    "severity_for",
]


#: Bands for a likelihood x impact score (1..25), lowest first. A score at or
#: above each bound earns that severity.
_SEVERITY_MEDIUM_AT = 6
_SEVERITY_HIGH_AT = 12
_SEVERITY_CRITICAL_AT = 20


@dataclass(frozen=True, slots=True)
class CategorySpec:
    """A budget line as authored by the catalogue, before maths is applied.

    Parameters
    ----------
    weight:
        Baseline relative share at the profile's reference guest count. Only
        *ratios* between weights matter - the allocator normalises them.
    kind, scale_exp:
        How the category behaves as guest count grows. ``scale_exp`` is the
        exponent in ``cost ~ guests ** scale_exp``: ``0.0`` for fixed-price
        packages, ``1.0`` for strictly per-guest, and something in between where
        economies of scale apply (a bigger mandap is not twice the price).
    low_pct, high_pct:
        The band a healthy plan keeps this category inside, as a percentage of
        the spendable budget. Exceeding a band raises a warning and triggers a
        bounded reallocation.
    demand_elasticity:
        How strongly the category's price rises in peak season (0..1).
    cut_consequence, cut_steps:
        Plain-language trade-off advice shown when the host is over budget.
    """

    key: str
    label: str
    weight: float
    kind: CategoryKind
    low_pct: float
    high_pct: float
    cut_consequence: str
    cut_steps: tuple[str, ...] = ()
    scale_exp: float = 0.5
    demand_elasticity: float = 0.05


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A timeline node, scheduled backwards from the event date."""

    id: str
    title: str
    offset_days: int
    duration_days: int = 1
    owner: OwnerRole = OwnerRole.HOST
    phase: str = "General"
    description: str = ""
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VendorSpec:
    """A vendor category with the paperwork a first-time host needs."""

    key: str
    label: str
    priority: Priority
    why: str
    book_by_offset_days: int
    deposit_pct: float = 25.0
    contract_musts: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    negotiation_tip: str = ""


@dataclass(frozen=True, slots=True)
class RiskSpec:
    """A pre-identified risk for this vertical."""

    key: str
    title: str
    likelihood: int
    impact: int
    mitigation: str
    owner: OwnerRole = OwnerRole.HOST
    trigger_sign: str = ""


@dataclass(frozen=True, slots=True)
class ThemeSpec:
    """A curated celebration concept used for offline personalisation.

    ``style_keywords`` drive similarity scoring against the host's
    :class:`~sutradhar.models.TasteProfile`; ``delta_pct`` estimates the extra
    budget pressure the concept introduces (negative means it saves money).
    """

    key: str
    name: str
    tagline: str
    palette: tuple[str, ...]
    decor: str
    menu: str
    favors: str
    attire: str
    music: str
    activities: str
    style_keywords: tuple[str, ...]
    suitable_for: tuple[EventType, ...]
    delta_pct: float = 0.0
    cost_tier: Priority = Priority.NORMAL


@dataclass(frozen=True, slots=True)
class RunOfShowSpec:
    """One row of the day-of (or day-before) run sheet."""

    start: time
    end: time
    title: str
    owner: OwnerRole = OwnerRole.PLANNER
    notes: str = ""
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class EventTypeProfile:
    """Everything Sutradhar knows about one celebration vertical."""

    event_type: EventType
    tagline: str
    reference_guests: int
    min_lead_days: int
    ideal_lead_days: int
    contingency_base_pct: float
    peak_months: tuple[int, ...]
    shoulder_months: tuple[int, ...] = ()
    categories: tuple[CategorySpec, ...] = ()
    tasks: tuple[TaskSpec, ...] = ()
    vendors: tuple[VendorSpec, ...] = ()
    risks: tuple[RiskSpec, ...] = ()
    themes: tuple[ThemeSpec, ...] = ()
    run_of_show: tuple[RunOfShowSpec, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def category(self, key: str) -> CategorySpec | None:
        """The budget category with ``key``, or ``None`` if absent."""
        return next((c for c in self.categories if c.key == key), None)

    def task(self, task_id: str) -> TaskSpec | None:
        """The task spec with ``id``, or ``None`` if absent."""
        return next((t for t in self.tasks if t.id == task_id), None)


def risk(
    key: str,
    title: str,
    likelihood: int,
    impact: int,
    mitigation: str,
    *,
    owner: OwnerRole = OwnerRole.HOST,
    trigger_sign: str = "",
) -> RiskSpec:
    """Small sugar for authoring risks with clamped scores."""
    return RiskSpec(
        key=key,
        title=title,
        likelihood=max(1, min(5, likelihood)),
        impact=max(1, min(5, impact)),
        mitigation=mitigation,
        owner=owner,
        trigger_sign=trigger_sign,
    )


def severity_for(score: int) -> RiskLevel:
    """Map a 1..25 likelihood x impact score onto an ordinal severity band."""
    if score >= _SEVERITY_CRITICAL_AT:
        return RiskLevel.CRITICAL
    if score >= _SEVERITY_HIGH_AT:
        return RiskLevel.HIGH
    if score >= _SEVERITY_MEDIUM_AT:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
