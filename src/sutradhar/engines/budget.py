"""Budget engine - turns a total into an exact, defensible allocation.

Why this engine exists at all: a language model cannot reliably add up. Ask one
for a line-item budget and the numbers will not reconcile - the columns drift by
a few thousand, the percentages do not reach 100, and nobody can audit it. So
the model is not asked. The maths happens here, deterministically, and the model
is instead invited to *comment* on a budget that already adds up.

The allocation runs in four stages:

1. **Weight** each category by its baseline share, adjusted for guest count
   (``guests ** scale_exp``), season demand, city tier and host priorities.
2. **Bound** the result so every category lands inside its benchmark band using
   a bounded water-filling pass.
3. **Integerise** with the largest-remainder method so the line items sum to the
   spendable total *exactly*, with zero rounding drift.
4. **Advise** - emit costed trade-offs a host can act on, plus warnings.

All money is handled in integer minor units. See :mod:`sutradhar.money`.
"""

from __future__ import annotations

import math
from datetime import date

from ..catalog.specs import CategorySpec, EventTypeProfile
from ..enums import Priority, RiskLevel, Season
from ..models import Brief, BudgetLine, BudgetPlan, PlanWarning, TradeOff
from ..money import Currency, format_minor
from .context import CostContext, cost_context

__all__ = ["TRIM_FRACTION", "allocate_budget", "allocate_minor", "bounded_shares"]

#: Fraction of a category we assume a realistic trim could free up.
TRIM_FRACTION = 0.15

#: Categories whose share of the budget grows in expensive metros. Rent, guest
#: hospitality and city logistics are what actually get pricier, not the cake.
_METRO_SENSITIVE = frozenset({"venue", "hospitality", "logistics", "production", "home_prep"})

#: How strongly the city tier re-weights those categories.
_TIER_STRENGTH = 0.35

#: Percentage-point slack allowed when checking a line against its band, so that
#: integer rounding never produces a spurious out-of-band warning.
_BAND_TOLERANCE = 0.75

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Core allocation maths
# --------------------------------------------------------------------------- #
def bounded_shares(
    shares: list[float],
    lows: list[float],
    highs: list[float],
) -> tuple[list[float], bool]:
    """Project ``shares`` onto the box ``[lows, highs]`` while preserving ratios.

    A bounded water-filling pass: allocate proportionally, clamp whichever
    categories overshoot a bound, then re-spend the freed budget across the
    remainder, repeating until nothing violates. Returns ``(shares, feasible)``
    where ``feasible`` is ``False`` only when the bands themselves cannot
    contain 1.0 (a catalogue-authoring bug, not a user error) - the caller turns
    that into a warning rather than a crash.
    """
    n = len(shares)
    if n == 0:
        return [], True

    lows = [max(0.0, lo) for lo in lows]
    highs = [max(hi, lo) for hi, lo in zip(highs, lows, strict=True)]

    feasible = (sum(lows) <= 1.0 + _EPS) and (sum(highs) >= 1.0 - _EPS)

    fixed: list[float | None] = [None] * n
    remaining = 1.0

    for _ in range(n + 1):
        free = [i for i in range(n) if fixed[i] is None]
        if not free:
            break

        total_weight = sum(shares[i] for i in free)
        if total_weight <= _EPS:
            # No signal left; split the remainder evenly across the free slots.
            per = remaining / len(free)
            for i in free:
                fixed[i] = per
            break

        clamped: list[int] = []
        for i in free:
            candidate = remaining * shares[i] / total_weight
            if candidate < lows[i] - _EPS:
                fixed[i] = lows[i]
                clamped.append(i)
            elif candidate > highs[i] + _EPS:
                fixed[i] = highs[i]
                clamped.append(i)

        if not clamped:
            for i in free:
                fixed[i] = remaining * shares[i] / total_weight
            break

        for i in clamped:
            remaining -= fixed[i] or 0.0
        if remaining < -_EPS:
            # The bands over-subscribe the budget; pin what is left at its floor.
            # The epsilon matters: clamping to a high of 0.9 leaves
            # `remaining == 0.09999999999999998`, and subtracting a low of 0.1
            # gives a tiny negative residue. Without the tolerance that reads as
            # over-subscription and wrongly reports feasible bands as infeasible.
            for i in free:
                if fixed[i] is None:
                    fixed[i] = lows[i]
            feasible = False
            break

    result = [float(value or 0.0) for value in fixed]

    # Absorb float drift into the largest element so the vector sums to exactly 1.
    total = sum(result)
    if total > 0 and abs(total - 1.0) > _EPS:
        biggest = max(range(n), key=result.__getitem__)
        result[biggest] += 1.0 - total

    return [max(0.0, value) for value in result], feasible


def allocate_minor(total_minor: int, fractions: list[float]) -> list[int]:
    """Split ``total_minor`` across ``fractions`` with zero rounding drift.

    Largest-remainder (Hare-Niemeyer) allocation: floor every product, then hand
    the leftover minor units to the largest fractional remainders. The result
    always sums to ``total_minor`` exactly, which is what lets a plan promise
    that every rupee is accounted for.
    """
    if not fractions:
        return []
    if total_minor <= 0:
        return [0] * len(fractions)

    weight_sum = sum(fractions)
    if weight_sum <= _EPS:
        return allocate_minor(total_minor, [1.0] * len(fractions))

    raw = [total_minor * (f / weight_sum) for f in fractions]
    base = [math.floor(value) for value in raw]
    leftover = total_minor - sum(base)

    order = sorted(range(len(raw)), key=lambda i: -(raw[i] - base[i]))
    for k in range(leftover):
        base[order[k % len(order)]] += 1
    return base


# --------------------------------------------------------------------------- #
# Weighting
# --------------------------------------------------------------------------- #
def _contingency_pct(profile: EventTypeProfile, lead_days: int) -> float:
    """Size the contingency buffer from how much slack the host has.

    Someone planning six weeks out cannot shop around, compare quotes or absorb
    a price rise, so they need a bigger cushion than a host with a year up their
    sleeve.
    """
    if lead_days >= 240:
        adjustment = -1.0
    elif lead_days >= 120:
        adjustment = 0.0
    elif lead_days >= 60:
        adjustment = 1.5
    else:
        adjustment = 3.0
    return round(max(3.0, min(15.0, profile.contingency_base_pct + adjustment)), 1)


def _category_weight(
    spec: CategorySpec,
    profile: EventTypeProfile,
    ctx: CostContext,
    priority: Priority,
) -> float:
    """Baseline weight adjusted for guests, season, city tier and priority."""
    guest_effect = (ctx.guests / profile.reference_guests) ** spec.scale_exp
    season_effect = 1.0 + (ctx.season_price_index - 1.0) * spec.demand_elasticity

    tier_effect = 1.0
    if spec.key in _METRO_SENSITIVE:
        tier_effect = 1.0 + (ctx.tier_index - 1.0) * _TIER_STRENGTH

    return float(spec.weight * guest_effect * season_effect * tier_effect * priority.weight)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def allocate_budget(
    brief: Brief,
    profile: EventTypeProfile,
    season: Season,
    *,
    currency: Currency,
    today: date | None = None,
) -> BudgetPlan:
    """Allocate ``brief.budget_total_minor`` across the profile's categories.

    Parameters
    ----------
    brief:
        The validated event brief.
    profile:
        Catalogue knowledge for this event type.
    season:
        Demand season, already resolved by
        :func:`sutradhar.catalog.registry.season_for`.
    currency:
        Currency the brief is denominated in.
    today:
        Reference date for lead-time maths. Defaults to the current date; tests
        pass an explicit value to stay deterministic.
    """
    ctx = cost_context(brief, season, today=today or date.today())
    guests = ctx.guests

    contingency_pct = _contingency_pct(profile, ctx.lead_days)
    contingency_minor = round(brief.budget_total_minor * contingency_pct / 100.0)
    spendable_minor = brief.budget_total_minor - contingency_minor
    if spendable_minor <= 0:
        # A budget too small to hold any contingency is still plan-able.
        contingency_minor = 0
        spendable_minor = brief.budget_total_minor

    priorities = brief.priorities or {}

    weights = [
        _category_weight(spec, profile, ctx, priorities.get(spec.key, Priority.NORMAL))
        for spec in profile.categories
    ]
    lows = [spec.low_pct / 100.0 for spec in profile.categories]
    highs = [spec.high_pct / 100.0 for spec in profile.categories]

    shares, _feasible = bounded_shares(weights, lows, highs)
    amounts = allocate_minor(spendable_minor, shares)

    lines: list[BudgetLine] = []
    for spec, amount, _share in zip(profile.categories, amounts, shares, strict=True):
        pct = (amount * 100.0 / spendable_minor) if spendable_minor else 0.0
        priority = priorities.get(spec.key, Priority.NORMAL)
        lines.append(
            BudgetLine(
                key=spec.key,
                label=spec.label,
                amount_minor=amount,
                pct=round(pct, 2),
                per_guest_minor=round(amount / guests),
                kind=spec.kind,
                rationale=_rationale(spec, ctx, priority),
                benchmark_low_pct=spec.low_pct,
                benchmark_high_pct=spec.high_pct,
                priority=priority,
                in_band=(spec.low_pct - _BAND_TOLERANCE)
                <= pct
                <= (spec.high_pct + _BAND_TOLERANCE),
            )
        )

    # Biggest line first reads naturally in every interface.
    lines.sort(key=lambda line: -line.amount_minor)

    warnings = _warnings(brief, profile, lines, ctx, spendable_minor)

    return BudgetPlan(
        currency=currency,
        total_minor=brief.budget_total_minor,
        spendable_minor=spendable_minor,
        contingency_minor=contingency_minor,
        allocated_minor=sum(amounts),
        per_guest_minor=round(brief.budget_total_minor / guests),
        lines=tuple(lines),
        trade_offs=tuple(_trade_offs(profile, lines, priorities)),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# Explanation & advice
# --------------------------------------------------------------------------- #
def _rationale(spec: CategorySpec, ctx: CostContext, priority: Priority) -> str:
    """Explain, in one sentence, *why* a line landed where it did."""
    if spec.scale_exp >= 0.95:
        basis = f"scales directly with your {ctx.guests} guests"
    elif spec.scale_exp <= 0.05:
        basis = "is a fixed-price package, so it does not grow with guest count"
    else:
        basis = "grows gently with guest count"

    season_note = ""
    if spec.demand_elasticity >= 0.15:
        if ctx.season is Season.PEAK:
            season_note = ", and is priced up in peak season"
        elif ctx.season is Season.OFF_PEAK:
            season_note = ", and is cheaper in the off-peak season"

    priority_note = f"; you marked it {priority.value}" if priority is not Priority.NORMAL else ""

    return f"{spec.label} {basis}{season_note}{priority_note}."


def _warnings(
    brief: Brief,
    profile: EventTypeProfile,
    lines: list[BudgetLine],
    ctx: CostContext,
    spendable_minor: int,
) -> list[PlanWarning]:
    """Surface genuinely actionable problems with the budget shape."""
    out: list[PlanWarning] = []

    starved = [
        line
        for line in lines
        if line.benchmark_low_pct > 0 and line.pct < line.benchmark_low_pct - _BAND_TOLERANCE
    ]
    if starved:
        per_guest = spendable_minor / max(1, ctx.guests)
        names = ", ".join(line.label for line in starved[:3])
        out.append(
            PlanWarning(
                code="categories_starved",
                message=(
                    f"At {format_minor(int(per_guest), brief.currency)} per guest, {names} "
                    f"fall below a workable minimum for a {profile.event_type.value}."
                ),
                severity=RiskLevel.HIGH,
                section="budget",
                suggestion="Raise the total, trim the guest list, or act on the trade-offs below.",
            )
        )

    if ctx.lead_days < profile.min_lead_days:
        out.append(
            PlanWarning(
                code="short_lead_time",
                message=(
                    f"Only {ctx.lead_days} days to plan, against a typical "
                    f"{profile.min_lead_days}-day minimum for a {profile.event_type.value}."
                ),
                severity=RiskLevel.HIGH,
                section="budget",
                suggestion="Lock the venue and the top two vendors first - everything else can flex.",
            )
        )
    elif ctx.season is Season.PEAK and any(
        spec.demand_elasticity >= 0.2 for spec in profile.categories
    ):
        out.append(
            PlanWarning(
                code="peak_pricing",
                message=(
                    f"This date falls in peak season, so venue-class costs typically run "
                    f"{round((ctx.season_price_index - 1) * 100)}% above the off-peak rate."
                ),
                severity=RiskLevel.MEDIUM,
                section="budget",
                suggestion="Compare at least three quotes and ask for an off-peak or weekday rate.",
            )
        )

    return out


def _trade_offs(
    profile: EventTypeProfile,
    lines: list[BudgetLine],
    priorities: dict[str, Priority],
) -> list[TradeOff]:
    """Propose costed, consequence-aware changes a host can actually make."""
    out: list[TradeOff] = []
    for line in lines:
        spec = profile.category(line.key)
        if spec is None or line.amount_minor <= 0:
            continue
        priority = priorities.get(line.key, line.priority)
        saving_minor = round(line.amount_minor * TRIM_FRACTION)
        if saving_minor <= 0:
            continue
        out.append(
            TradeOff(
                action="trim",
                category_key=line.key,
                category_label=line.label,
                saving_minor=saving_minor,
                impact=_impact_for(priority),
                consequence=spec.cut_consequence,
                steps=spec.cut_steps[:2],
            )
        )

    out.sort(key=lambda t: (-t.saving_minor, t.category_label))
    return out[:5]


def _impact_for(priority: Priority) -> RiskLevel:
    """Map how much a host cares about a category onto the cost of cutting it."""
    return {
        Priority.VITAL: RiskLevel.CRITICAL,
        Priority.HIGH: RiskLevel.HIGH,
        Priority.NORMAL: RiskLevel.MEDIUM,
        Priority.LOW: RiskLevel.LOW,
    }[priority]
