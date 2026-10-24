"""Risk engine - a scored, contextual risk register with mitigations.

The catalogue ships the risks that *always* apply to a vertical. This engine
then does two things a static list cannot: it re-scores those risks against the
host's actual context (an outdoor wedding in peak season is a different risk
profile from an indoor winter one), and it appends risks that only exist
because of that context - planning in six weeks, or twice the reference guest
count.
"""

from __future__ import annotations

from datetime import date

from ..catalog.specs import EventTypeProfile, severity_for
from ..enums import OwnerRole, RiskLevel, WindExposure
from ..models import Brief, PlanWarning, RiskItem, RiskPlan
from .context import CostContext, cost_context

__all__ = ["build_risks"]

#: Below this many days, availability risk rises sharply for every vertical.
_SHORT_LEAD_DAYS = 30

#: Guest counts above this multiple of the profile's reference trigger scale risk.
_SCALE_CREEP_RATIO = 2.0


def build_risks(
    brief: Brief,
    profile: EventTypeProfile,
    season,  # type: ignore[explicit-override]
    *,
    today: date | None = None,
) -> RiskPlan:
    """Score the catalogue's risks against ``brief`` and add contextual ones."""
    ctx = cost_context(brief, season, today=today or date.today())

    items: list[RiskItem] = []
    for spec in profile.risks:
        likelihood = spec.likelihood
        likelihood += _contextual_pressure(spec.key, spec.title, ctx, profile)
        likelihood = max(1, min(5, likelihood))

        items.append(
            RiskItem(
                key=spec.key,
                title=spec.title,
                likelihood=likelihood,
                impact=spec.impact,
                severity=severity_for(likelihood * spec.impact),
                mitigation=spec.mitigation,
                owner=spec.owner,
                trigger_sign=spec.trigger_sign,
            )
        )

    items.extend(_contextual_risks(ctx, profile))
    items.sort(key=lambda r: (-r.score, r.title))
    return RiskPlan(items=tuple(items))


def _contextual_pressure(
    key: str,
    title: str,
    ctx: CostContext,
    profile: EventTypeProfile,
) -> int:
    """How much the host's specific context raises this risk's likelihood."""
    pressure = 0
    text = f"{key} {title}".lower()

    is_weather = any(word in text for word in ("rain", "weather", "monsoon", "heat"))
    if is_weather and ctx.is_outdoor and ctx.setting is WindExposure.OPEN_AIR:
        pressure += 1
    elif is_weather and ctx.is_outdoor:
        pressure += 1

    is_availability = any(word in text for word in ("noshow", "no show", "cancel", "permit", "vendor"))
    if is_availability and ctx.lead_days < profile.min_lead_days:
        pressure += 1
    if is_availability and ctx.season.value == "peak":
        pressure += 1

    if "budget" in text or "overrun" in text:
        if ctx.lead_days < 60:
            pressure += 1

    return pressure


def _contextual_risks(ctx: CostContext, profile: EventTypeProfile) -> list[RiskItem]:
    """Risks that exist only because of this host's particular situation."""
    out: list[RiskItem] = []

    if ctx.lead_days < _SHORT_LEAD_DAYS:
        out.append(
            RiskItem(
                key="rush_fees",
                title=f"Only {max(ctx.lead_days, 0)} days to plan - rush fees and reduced choice",
                likelihood=5,
                impact=3,
                severity=severity_for(15),
                mitigation=(
                    "Lock the three essential vendors first, accept the best available rather than "
                    "the perfect option, and pay for the cancellation flexibility you lose by being late."
                ),
                owner=OwnerRole.PLANNER,
                trigger_sign="Any vendor quotes a premium for the short window",
            )
        )

    if ctx.guests > profile.reference_guests * _SCALE_CREEP_RATIO:
        out.append(
            RiskItem(
                key="scale_creep",
                title=f"{ctx.guests} guests against a {profile.reference_guests}-guest reference plan",
                likelihood=4,
                impact=3,
                severity=severity_for(12),
                mitigation=(
                    "Re-cost catering and seating at the real number before signing, and hold a 5% "
                    "RSVP buffer so the final count is not a surprise."
                ),
                owner=OwnerRole.FINANCE,
                trigger_sign="The guest list is still growing inside the final month",
            )
        )

    if ctx.is_outdoor and ctx.setting is WindExposure.OPEN_AIR:
        out.append(
            RiskItem(
                key="wind",
                title="Wind affecting paper, drapery and open flames",
                likelihood=3,
                impact=3,
                severity=severity_for(9),
                mitigation=(
                    "Specify weighted bases for every structure, swap open flames for LED candles, "
                    "and ask the decorator to certify the rigging in writing."
                ),
                owner=OwnerRole.PLANNER,
                trigger_sign="Any forecast wind above 25 km/h on the event day",
            )
        )

    return out


def risk_warnings(plan: RiskPlan) -> tuple[PlanWarning, ...]:
    """Translate the top risks into plan-level warnings for the host."""
    warnings: list[PlanWarning] = []
    for item in plan.top(3):
        if item.severity is RiskLevel.LOW:
            continue
        warnings.append(
            PlanWarning(
                code=f"risk_{item.key}",
                message=item.title,
                severity=item.severity,
                section="risks",
                suggestion=item.mitigation,
            )
        )
    return tuple(warnings)
