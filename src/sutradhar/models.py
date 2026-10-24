"""Domain models - the output contract of every planning engine.

These are plain frozen dataclasses: no behaviour beyond validation and a couple
of derived properties. Keeping the models dumb and the engines smart is what
makes the whole system easy to test (assert on data) and easy to re-skin (the
CLI, the web server and the exporters all consume these exact types).

Money is always an ``int`` number of *minor units* - see :mod:`sutradhar.money`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from .enums import (
    CategoryKind,
    CityTier,
    EventType,
    OwnerRole,
    Priority,
    RiskLevel,
    Season,
    Source,
    TaskStatus,
    WindExposure,
)
from .exceptions import BriefError
from .money import Currency
from .serialise import dumps, with_computed

__all__ = [
    "TasteProfile",
    "Brief",
    "PlanWarning",
    "BudgetLine",
    "TradeOff",
    "BudgetPlan",
    "Task",
    "RunOfShowSlot",
    "TimelinePlan",
    "VendorItem",
    "PaymentMilestone",
    "VendorPlan",
    "RiskItem",
    "RiskPlan",
    "IdeaConcept",
    "Plan",
]

# Hard guards so a hostile or typo'd input can never allocate absurd work.
MAX_GUESTS = 100_000
MAX_TITLE_LEN = 120
MAX_NOTES_LEN = 2_000


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TasteProfile:
    """The host's aesthetic, cultural and dietary preferences.

    Every field is free-form on purpose: the personalisation engine scores
    overlap against a curated catalogue (offline) and feeds the same signals to
    the language model (online), so a profile written in anyone's own words
    still produces relevant ideas.
    """

    _COMPUTED = ("is_empty",)

    palette: tuple[str, ...] = ()
    style_keywords: tuple[str, ...] = ()
    traditions: tuple[str, ...] = ()
    vibe: str = ""
    dietary: tuple[str, ...] = ()
    accessibility_notes: tuple[str, ...] = ()
    must_haves: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.palette, self.style_keywords, self.traditions, self.vibe,
             self.dietary, self.accessibility_notes, self.must_haves, self.avoid)
        )

    def signal_tokens(self) -> set[str]:
        """Lower-cased keyword bag used for catalogue similarity scoring."""
        tokens: set[str] = set()
        for group in (
            self.palette,
            self.style_keywords,
            self.traditions,
            self.dietary,
            self.must_haves,
        ):
            for item in group:
                tokens.update(t for t in item.lower().replace("/", " ").split() if len(t) > 2)
        if self.vibe:
            tokens.update(t for t in self.vibe.lower().split() if len(t) > 2)
        return tokens - {w for w in ("and", "the", "with", "for")}


@dataclass(frozen=True, slots=True)
class Brief:
    """Everything the planner needs to know about one celebration."""

    event_type: EventType
    event_date: date
    guest_count: int
    budget_total_minor: int
    currency: Currency = Currency.INR
    city_tier: CityTier = CityTier.TIER_1
    location: str = ""
    title: str = ""
    host_name: str = ""
    setting: WindExposure = WindExposure.INDOOR
    season_override: Season | None = None
    priorities: dict[str, Priority] = field(default_factory=dict)
    taste: TasteProfile = field(default_factory=TasteProfile)
    notes: str = ""
    natural_language: str = ""

    _COMPUTED = ("budget_total", "display_title")

    # -- derived ------------------------------------------------------------ #
    @property
    def budget_total(self) -> float:
        return self.budget_total_minor / (10 ** self.currency.info.decimals)

    @property
    def display_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        return f"{self.event_type.value.title()} on {self.event_date:%d %b %Y}"

    # -- validation --------------------------------------------------------- #
    def validate(self) -> None:
        """Raise :class:`BriefError` on impossible input.

        Called by the pipeline before any engine runs, so engines can assume a
        well-formed brief and stay free of defensive noise.
        """
        if not (1 <= self.guest_count <= MAX_GUESTS):
            raise BriefError(
                "guest_count",
                f"guest count must be between 1 and {MAX_GUESTS:,}; got {self.guest_count}",
            )
        if self.budget_total_minor <= 0:
            raise BriefError("budget_total_minor", "total budget must be greater than zero")
        if len(self.title) > MAX_TITLE_LEN:
            raise BriefError("title", f"title must be {MAX_TITLE_LEN} characters or fewer")
        if len(self.notes) > MAX_NOTES_LEN:
            raise BriefError("notes", f"notes must be {MAX_NOTES_LEN} characters or fewer")
        if self.event_date is None:
            raise BriefError("event_date", "an event date is required")
        for key, priority in self.priorities.items():
            if not isinstance(priority, Priority):
                raise BriefError(f"priorities.{key}", f"unknown priority {priority!r}")

    def to_dict(self) -> dict[str, Any]:
        return with_computed(self)


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PlanWarning:
    """A non-fatal, actionable observation about a plan."""

    code: str
    message: str
    severity: RiskLevel = RiskLevel.MEDIUM
    section: str = "plan"
    suggestion: str = ""


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BudgetLine:
    """One allocated budget category."""

    key: str
    label: str
    amount_minor: int
    pct: float
    per_guest_minor: int
    kind: CategoryKind
    rationale: str = ""
    benchmark_low_pct: float = 0.0
    benchmark_high_pct: float = 10.0
    priority: Priority = Priority.NORMAL
    in_band: bool = True

    _COMPUTED = ("amount",)

    @property
    def amount(self) -> float:
        return self.amount_minor / 100.0


@dataclass(frozen=True, slots=True)
class TradeOff:
    """A concrete, costed way to change the plan - and what it costs the guest."""

    action: str
    category_key: str
    category_label: str
    saving_minor: int
    impact: RiskLevel
    consequence: str
    steps: tuple[str, ...] = ()

    _COMPUTED = ("saving",)

    @property
    def saving(self) -> float:
        return self.saving_minor / 100.0


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    """The full, exactly-reconciled allocation."""

    currency: Currency
    total_minor: int
    spendable_minor: int
    contingency_minor: int
    allocated_minor: int
    per_guest_minor: int
    lines: tuple[BudgetLine, ...]
    trade_offs: tuple[TradeOff, ...] = ()
    warnings: tuple[PlanWarning, ...] = ()

    _COMPUTED = ("unallocated_minor", "total_committed_minor", "contingency_pct")

    @property
    def unallocated_minor(self) -> int:
        """Spendable money left unassigned. Always ``0``.

        The allocator guarantees exact reconciliation, so anything non-zero here
        would be a bug - which makes this a genuinely useful assertion both in
        the test suite and at runtime.
        """
        return self.spendable_minor - self.allocated_minor

    @property
    def total_committed_minor(self) -> int:
        """Everything the host will actually spend: line items plus contingency."""
        return self.allocated_minor + self.contingency_minor

    @property
    def contingency_pct(self) -> float:
        if self.total_minor == 0:
            return 0.0
        return round(self.contingency_minor * 100.0 / self.total_minor, 2)

    def line(self, key: str) -> BudgetLine | None:
        return next((ln for ln in self.lines if ln.key == key), None)


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Task:
    """A single scheduled work item on the critical-path timeline.

    ``offset_days`` is relative to the event date and is negative for advance
    preparation, zero for day-of work and positive for wrap-up.
    """

    id: str
    title: str
    offset_days: int
    due: date
    owner: OwnerRole = OwnerRole.HOST
    phase: str = "General"
    description: str = ""
    duration_days: int = 1
    depends_on: tuple[str, ...] = ()
    slack_days: int = 0
    earliest_start: date | None = None
    latest_start: date | None = None
    critical: bool = False
    status: TaskStatus = TaskStatus.NOT_STARTED

    _COMPUTED = ("days_before_event",)

    @property
    def days_before_event(self) -> int:
        return max(0, -self.offset_days)


@dataclass(frozen=True, slots=True)
class RunOfShowSlot:
    """One row of the day-of run sheet.

    ``day_offset`` supports multi-day formats: ``0`` is the main day, ``-1`` the
    day before, and so on.
    """

    start: time
    end: time
    title: str
    owner: OwnerRole = OwnerRole.PLANNER
    notes: str = ""
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class TimelinePlan:
    """Scheduled tasks plus the critical path and a day-of run sheet."""

    event_date: date
    lead_days: int
    min_required_lead_days: int
    feasible: bool
    tasks: tuple[Task, ...]
    critical_path: tuple[str, ...] = ()
    run_of_show: tuple[RunOfShowSlot, ...] = ()
    season: Season = Season.OFF_PEAK
    warnings: tuple[PlanWarning, ...] = ()

    _COMPUTED = ("critical_load_days",)

    def task(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    @property
    def critical_load_days(self) -> int:
        return sum(t.duration_days for t in self.tasks if t.critical)


# --------------------------------------------------------------------------- #
# Vendors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class VendorItem:
    """A vendor category with its contract checklist and payment shape."""

    key: str
    label: str
    priority: Priority
    why: str
    book_by_offset_days: int
    book_by: date
    deposit_pct: float
    contract_musts: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    negotiation_tip: str = ""


@dataclass(frozen=True, slots=True)
class PaymentMilestone:
    """One dated payment obligation across all vendors."""

    vendor_key: str
    label: str
    due: date
    pct: float
    amount_minor: int


@dataclass(frozen=True, slots=True)
class VendorPlan:
    """Vendor checklist plus the consolidated deposit cash-flow schedule."""

    items: tuple[VendorItem, ...]
    milestones: tuple[PaymentMilestone, ...] = ()
    deposit_total_minor: int = 0

    _COMPUTED = ("must_have_count",)

    def item(self, key: str) -> VendorItem | None:
        return next((v for v in self.items if v.key == key), None)

    @property
    def must_have_count(self) -> int:
        return sum(1 for v in self.items if v.priority in (Priority.HIGH, Priority.VITAL))


# --------------------------------------------------------------------------- #
# Risks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RiskItem:
    """A scored risk with a mitigation the host can actually perform."""

    key: str
    title: str
    likelihood: int  # 1..5
    impact: int  # 1..5
    severity: RiskLevel
    mitigation: str
    owner: OwnerRole = OwnerRole.HOST
    trigger_sign: str = ""

    _COMPUTED = ("score",)

    @property
    def score(self) -> int:
        return self.likelihood * self.impact


@dataclass(frozen=True, slots=True)
class RiskPlan:
    """The scored risk register for one event."""

    items: tuple[RiskItem, ...]

    _COMPUTED = ("residual_score",)

    @property
    def residual_score(self) -> int:
        """Total open exposure - higher means the plan carries more risk."""
        return sum(r.score for r in self.items if r.severity is not RiskLevel.LOW)

    def top(self, n: int = 3) -> tuple[RiskItem, ...]:
        return tuple(sorted(self.items, key=lambda r: (-r.score, r.title))[:n])


# --------------------------------------------------------------------------- #
# Personalised ideas
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IdeaConcept:
    """One personalised celebration concept, grounded against the budget."""

    key: str
    name: str
    tagline: str
    palette: tuple[str, ...] = ()
    decor: str = ""
    menu: str = ""
    favors: str = ""
    attire: str = ""
    music: str = ""
    activities: str = ""
    estimated_delta_pct: float = 0.0
    cost_fit: str = ""
    source: Source = Source.CATALOG
    grounded: bool = True
    rationale: str = ""

    _COMPUTED = ("within_budget",)

    @property
    def within_budget(self) -> bool:
        return self.grounded


# --------------------------------------------------------------------------- #
# Aggregate plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Plan:
    """A complete celebration plan - the single object every interface renders."""

    brief: Brief
    budget: BudgetPlan
    timeline: TimelinePlan
    vendors: VendorPlan
    risks: RiskPlan
    ideas: tuple[IdeaConcept, ...] = ()
    warnings: tuple[PlanWarning, ...] = ()
    provenance: dict[str, Source] = field(default_factory=dict)
    generated_at: datetime | None = None
    plan_version: str = "1.0"
    ai_provider: str = "none"
    ai_used: bool = False

    _COMPUTED = ("all_warnings",)

    @property
    def all_warnings(self) -> tuple[PlanWarning, ...]:
        """Warnings from every section plus the plan-level ones, de-duplicated."""
        seen: dict[str, PlanWarning] = {}
        for w in (
            *self.warnings,
            *self.budget.warnings,
            *self.timeline.warnings,
        ):
            seen.setdefault(f"{w.section}:{w.code}:{w.message}", w)
        return tuple(seen.values())

    def to_dict(self) -> dict[str, Any]:
        return with_computed(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return dumps(self, indent=indent)
