"""The planning pipeline - one call that turns a brief into a complete plan.

This is the only place the pieces are wired together, and the wiring order is
deliberate:

    brief -> budget -> timeline -> vendors -> risks -> ideas -> plan

Each stage depends on a real number produced by the stage before it. Vendors
need the budget to schedule payments against; ideas need the budget to be
grounded; risks need the season and lead time already computed. That dependency
chain is why the model is consulted *last* and only for prose: by the time it
runs, every number it might mention already exists and has been reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .ai.ideas import generate_ideas
from .ai.provider import Provider, resolve_provider
from .catalog.registry import get_profile, season_for
from .config import Settings, load_settings
from .engines.budget import allocate_budget
from .engines.risk import build_risks, risk_warnings
from .engines.timeline import build_timeline
from .engines.vendors import build_vendors
from .enums import RiskLevel, Source
from .models import Brief, Plan, PlanWarning

__all__ = ["Planner", "plan_event"]


@dataclass(slots=True)
class Planner:
    """Reusable planner bound to one configuration.

    Holding the resolved provider on the instance means the (potentially slow)
    provider selection happens once, and callers can inject a fake provider in
    tests without touching global state.
    """

    settings: Settings = field(default_factory=load_settings)
    provider: Provider | None = None
    today: date | None = None

    def __post_init__(self) -> None:
        if self.provider is None:
            self.provider = resolve_provider(self.settings)

    def plan(self, brief: Brief) -> Plan:
        """Produce a complete plan for ``brief``."""
        assert self.provider is not None, "provider must be resolved before planning"
        today = self.today or date.today()

        brief.validate()
        profile = get_profile(brief.event_type)
        season = season_for(
            brief.event_type, brief.event_date.month, override=brief.season_override
        )

        budget = allocate_budget(brief, profile, season, currency=brief.currency, today=today)
        timeline = build_timeline(brief, profile, season, today=today)
        vendors = build_vendors(
            brief,
            profile,
            season,
            budget_amounts={line.key: line.amount_minor for line in budget.lines},
            today=today,
        )
        risks = build_risks(brief, profile, season, today=today)
        idea_result = generate_ideas(brief, budget, profile, self.provider, self.settings)

        provenance = {
            "budget": Source.ENGINE,
            "timeline": Source.ENGINE,
            "vendors": Source.ENGINE,
            "risks": Source.ENGINE,
            "ideas": Source.MODEL if idea_result.used_model else Source.CATALOG,
        }

        warnings: list[PlanWarning] = list(idea_result.warnings)
        accessibility = _accessibility_warning(brief)
        if accessibility is not None:
            warnings.append(accessibility)
        warnings.extend(risk_warnings(risks))

        return Plan(
            brief=brief,
            budget=budget,
            timeline=timeline,
            vendors=vendors,
            risks=risks,
            ideas=idea_result.ideas,
            warnings=tuple(warnings),
            provenance=provenance,
            generated_at=datetime.now(timezone.utc),
            plan_version="1.0",
            ai_provider=idea_result.provider_name,
            ai_used=idea_result.used_model,
        )


def plan_event(
    brief: Brief,
    *,
    settings: Settings | None = None,
    provider: Provider | None = None,
    today: date | None = None,
) -> Plan:
    """Convenience wrapper for one-shot planning.

    ``plan_event(brief)`` is all a library consumer needs; the :class:`Planner`
    class exists for callers who plan repeatedly and want to reuse configuration.
    """
    return Planner(settings=settings or load_settings(), provider=provider, today=today).plan(brief)


def _accessibility_warning(brief: Brief) -> PlanWarning | None:
    """Prompt for accessibility when a large or public event has not mentioned it."""
    notes = brief.taste.accessibility_notes
    if notes:
        return None
    if brief.event_type.value != "community" and brief.guest_count < 100:
        return None
    return PlanWarning(
        code="accessibility_not_stated",
        message=(
            "No accessibility requirements were recorded, and this is a "
            f"{brief.guest_count}-guest event."
        ),
        severity=RiskLevel.MEDIUM,
        section="plan",
        suggestion=(
            "Confirm step-free access, seating for elderly guests, allergen labelling and "
            "a quiet space before you book the venue."
        ),
    )
