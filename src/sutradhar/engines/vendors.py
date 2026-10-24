"""Vendor engine - checklists, contract red flags and a cash-flow schedule.

Two things make this engine more than a static checklist:

* **Timing is derived, not asserted.** Every vendor's "book by" date comes from
  the same event date the timeline is anchored to, so the checklist and the
  schedule can never disagree.
* **Cash flow is modelled.** Each vendor's budget allocation is split across
  dated payment milestones, so a host sees the *shape* of the money they need
  before the day - not just a total. This is the single most common thing a
  first-time host gets wrong.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..catalog.specs import EventTypeProfile
from ..enums import EventType, Priority, RiskLevel
from ..models import Brief, PaymentMilestone, PlanWarning, VendorItem, VendorPlan
from .budget import allocate_minor
from .context import CostContext, cost_context

__all__ = ["build_vendors"]

#: Bridge from a vendor category to the budget category that funds it. Vendor
#: labels are what a host thinks in ("the caterer"); budget labels are what the
#: allocator produces ("Catering & Beverages"). Keeping the mapping explicit
#: here - rather than guessing from names - is what lets the deposit schedule
#: reconcile against the budget exactly.
_VENDOR_BUDGET_MAP: dict[EventType, dict[str, str]] = {
    EventType.WEDDING: {
        "venue": "venue", "caterer": "catering", "decorator": "decor",
        "photographer": "photography", "makeup": "beauty", "dj": "entertainment",
        "invitations": "invites", "transport": "transport",
    },
    EventType.BIRTHDAY: {
        "venue": "venue", "caterer": "food", "entertainer": "activities",
        "decorator": "decor", "photographer": "photography", "invitations": "invites",
    },
    EventType.COMMUNITY: {
        "venue": "venue", "av": "production", "catering": "food",
        "insurance": "safety", "printing": "outreach", "sanitation": "logistics",
        "security": "logistics", "accessibility": "accessibility",
    },
    EventType.HOUSEWARMING: {
        "caterer": "food", "cleaning": "home_prep", "rentals": "hospitality",
        "florist": "decor", "music": "entertainment",
    },
    EventType.ANNIVERSARY: {
        "venue": "venue", "caterer": "food", "florist": "decor",
        "photographer": "photography", "music": "entertainment",
    },
}

#: Days before the event that the balance is settled.
_BALANCE_OFFSET_DAYS = 3
#: Days before the event that a progress payment lands.
_PROGRESS_OFFSET_DAYS = 30
#: Share taken as a progress payment where the deposit leaves room for one.
_PROGRESS_PCT = 30.0


def build_vendors(
    brief: Brief,
    profile: EventTypeProfile,
    season,  # type: ignore[explicit-override]
    *,
    budget_amounts: dict[str, int],
    today: date | None = None,
) -> VendorPlan:
    """Assemble the vendor checklist and its dated payment schedule.

    Parameters
    ----------
    budget_amounts:
        ``{category_key: amount_minor}`` from the finished budget, used to
        reconcile each vendor's payments against real allocated money.
    """
    ctx = cost_context(brief, season, today=today or date.today())
    mapping = _VENDOR_BUDGET_MAP.get(profile.event_type, {})

    items: list[VendorItem] = []
    milestones: list[PaymentMilestone] = []

    for spec in profile.vendors:
        book_by = date.fromordinal(ctx.event_date.toordinal() - spec.book_by_offset_days)
        items.append(
            VendorItem(
                key=spec.key,
                label=spec.label,
                priority=spec.priority,
                why=spec.why,
                book_by_offset_days=spec.book_by_offset_days,
                book_by=book_by,
                deposit_pct=spec.deposit_pct,
                contract_musts=spec.contract_musts,
                red_flags=spec.red_flags,
                questions=spec.questions,
                negotiation_tip=spec.negotiation_tip,
            )
        )
        milestones.extend(
            _milestones_for(spec.key, spec.deposit_pct, book_by, ctx.event_date,
                            budget_amounts.get(mapping.get(spec.key, ""), 0))
        )

    milestones.sort(key=lambda m: (m.due, m.vendor_key, m.label))

    warnings = _warnings(ctx, profile, items, milestones)
    return VendorPlan(
        items=tuple(items),
        milestones=tuple(milestones),
        deposit_total_minor=sum(m.amount_minor for m in milestones),
    )


def _milestones_for(
    vendor_key: str,
    deposit_pct: float,
    book_by: date,
    event_date: date,
    category_minor: int,
) -> list[PaymentMilestone]:
    """Split a vendor's allocation across dated payments totalling 100%.

    The shape is the industry standard: an advance to hold the date, an optional
    progress payment, and the balance shortly before the event. Paying the
    balance late is deliberate leverage - it is the host's only real remedy if a
    vendor under-delivers.
    """
    deposit = max(0.0, min(100.0, deposit_pct))
    progress = _PROGRESS_PCT if deposit + _PROGRESS_PCT <= 90.0 else 0.0
    balance = max(0.0, 100.0 - deposit - progress)

    plan: list[tuple[str, date, float]] = [("Booking advance", book_by, deposit)]
    if progress:
        plan.append(("Progress payment", date.fromordinal(event_date.toordinal() - _PROGRESS_OFFSET_DAYS), progress))
    plan.append(
        ("Balance before the event", date.fromordinal(event_date.toordinal() - _BALANCE_OFFSET_DAYS), balance)
    )

    weights = [pct for _, _, pct in plan]
    amounts = allocate_minor(category_minor, weights)

    return [
        PaymentMilestone(
            vendor_key=vendor_key,
            label=label,
            due=due,
            pct=round(pct, 1),
            amount_minor=amount,
        )
        for (label, due, pct), amount in zip(plan, amounts, strict=True)
    ]


def _warnings(
    ctx: CostContext,
    profile: EventTypeProfile,
    items: list[VendorItem],
    milestones: list[PaymentMilestone],
) -> list[PlanWarning]:
    out: list[PlanWarning] = []

    heavy = [item for item in items if item.deposit_pct >= 40.0]
    if heavy:
        names = ", ".join(item.label for item in heavy[:3])
        out.append(
            PlanWarning(
                code="heavy_deposits",
                message=(
                    f"{len(heavy)} vendor(s) ask for 40% or more up front ({names}). "
                    "That is a large commitment before you have seen any work."
                ),
                severity=RiskLevel.MEDIUM,
                section="vendors",
                suggestion="Ask to move deposits to 25-30% and tie the rest to delivery milestones.",
            )
        )

    late_bookers = [item for item in items if item.priority in (Priority.HIGH, Priority.VITAL)
                    and item.book_by < ctx.today]
    if late_bookers:
        names = ", ".join(item.label for item in late_bookers[:3])
        out.append(
            PlanWarning(
                code="vendors_overdue",
                message=f"Booking windows have already passed for: {names}.",
                severity=RiskLevel.HIGH,
                section="vendors",
                suggestion="Call these vendors this week and expect a rush fee or a reduced choice.",
            )
        )

    must_have = sum(1 for item in items if item.priority in (Priority.HIGH, Priority.VITAL))
    out.append(
        PlanWarning(
            code="vendor_count",
            message=(
                f"{len(items)} vendor categories to line up, {must_have} of them essential. "
                f"Payments total {len(milestones)} dated milestones."
            ),
            severity=RiskLevel.LOW,
            section="vendors",
            suggestion="Work top-down by priority - a plan with the three essentials booked is already safe.",
        )
    )
    return out
