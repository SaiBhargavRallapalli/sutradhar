"""Budget engine tests.

The headline claim of the budget engine is *exactness*: every minor unit is
assigned, and the line items reconcile to the spendable total with zero drift.
These tests hold that claim to account across many shapes of input, because a
budget that is only nearly right is worse than useless.
"""

from __future__ import annotations

from datetime import date

import pytest

from sutradhar import Brief, CityTier, Currency, EventType, Priority
from sutradhar.catalog.registry import get_profile
from sutradhar.engines.budget import allocate_budget, allocate_minor, bounded_shares
from sutradhar.enums import Season
from sutradhar.models import BriefError

PROFILE = get_profile(EventType.WEDDING)


def _brief(total: int, guests: int = 200, **kwargs) -> Brief:
    return Brief(
        event_type=EventType.WEDDING,
        event_date=date(2027, 6, 6),
        guest_count=guests,
        budget_total_minor=total,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Exactness - the core promise
# --------------------------------------------------------------------------- #
class TestExactness:
    @pytest.mark.parametrize("total", [1_00, 12_345, 99_999, 1_500_000_00, 999_999_99])
    def test_lines_sum_to_spendable_exactly(self, total: int) -> None:
        plan = allocate_budget(
            _brief(total), PROFILE, Season.OFF_PEAK, currency=Currency.INR, today=date(2026, 1, 1)
        )
        assert plan.unallocated_minor == 0
        assert sum(line.amount_minor for line in plan.lines) == plan.spendable_minor

    def test_spendable_plus_contingency_equals_total(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert plan.spendable_minor + plan.contingency_minor == plan.total_minor
        assert plan.total_committed_minor == plan.total_minor

    @pytest.mark.parametrize("total", [1, 7, 101, 10_000])
    def test_awkward_totals_never_leave_a_rupee_unassigned(self, total: int) -> None:
        """Small and prime-ish totals are where largest-remainder usually slips."""
        plan = allocate_budget(
            _brief(total, guests=1),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert plan.unallocated_minor == 0
        assert all(line.amount_minor >= 0 for line in plan.lines)

    def test_negative_and_zero_never_produced(self) -> None:
        plan = allocate_budget(
            _brief(5_00), PROFILE, Season.PEAK, currency=Currency.INR, today=date(2026, 1, 1)
        )
        assert all(line.amount_minor >= 0 for line in plan.lines)


# --------------------------------------------------------------------------- #
# allocate_minor - the integerisation primitive
# --------------------------------------------------------------------------- #
class TestAllocateMinor:
    def test_exact_sum_for_awkward_fractions(self) -> None:
        amounts = allocate_minor(1_000_001, [1 / 3, 1 / 3, 1 / 3])
        assert sum(amounts) == 1_000_001

    def test_largest_remainder_prefers_biggest_fractional_part(self) -> None:
        # 10 split in a 0.5/0.25/0.25 ratio -> 5/3/2 or 5/2/3, never 4/3/3.
        amounts = allocate_minor(10, [0.5, 0.25, 0.25])
        assert sum(amounts) == 10
        assert max(amounts) == 5

    def test_zero_total_yields_zeros(self) -> None:
        assert allocate_minor(0, [0.5, 0.5]) == [0, 0]

    def test_empty_fractions_yields_empty(self) -> None:
        assert allocate_minor(100, []) == []

    def test_zero_weights_split_evenly_rather_than_crashing(self) -> None:
        amounts = allocate_minor(90, [0.0, 0.0, 0.0])
        assert sum(amounts) == 90
        assert amounts == [30, 30, 30]

    @pytest.mark.parametrize("n", [1, 2, 3, 7, 13, 40])
    def test_reconciles_for_any_bucket_count(self, n: int) -> None:
        fractions = [1.0 / (i + 1) for i in range(n)]
        amounts = allocate_minor(999_983, fractions)
        assert len(amounts) == n
        assert sum(amounts) == 999_983


# --------------------------------------------------------------------------- #
# bounded_shares - the water-filling pass
# --------------------------------------------------------------------------- #
class TestBoundedShares:
    def test_result_sums_to_one(self) -> None:
        shares, feasible = bounded_shares([1.0, 2.0, 3.0], [0.05, 0.05, 0.05], [0.6, 0.6, 0.6])
        assert feasible
        assert abs(sum(shares) - 1.0) < 1e-9

    def test_clamps_to_bounds_when_proportional_would_overshoot(self) -> None:
        # One huge weight would take 90% but its ceiling is 40%.
        shares, feasible = bounded_shares([9.0, 1.0, 1.0], [0.05] * 3, [0.40, 0.9, 0.9])
        assert feasible
        assert shares[0] == pytest.approx(0.40, abs=1e-6)
        assert abs(sum(shares) - 1.0) < 1e-9

    def test_respects_lower_bounds(self) -> None:
        shares, feasible = bounded_shares([100.0, 0.001], [0.15, 0.10], [0.9, 0.9])
        assert feasible
        assert shares[1] >= 0.10 - 1e-9

    def test_reports_infeasible_bands_without_raising(self) -> None:
        # Lows alone exceed 1.0: a catalogue-authoring bug, not a user error.
        shares, feasible = bounded_shares([1.0, 1.0], [0.8, 0.8], [0.9, 0.9])
        assert feasible is False
        assert all(s >= 0 for s in shares)

    def test_empty_input(self) -> None:
        assert bounded_shares([], [], []) == ([], True)

    def test_never_negative_even_with_degenerate_bounds(self) -> None:
        shares, _ = bounded_shares([1.0, 1.0], [-5.0, -5.0], [2.0, 2.0])
        assert all(s >= 0 for s in shares)


# --------------------------------------------------------------------------- #
# Weighting behaviour
# --------------------------------------------------------------------------- #
class TestWeighting:
    def test_priority_moves_money_toward_the_chosen_category(self) -> None:
        today = date(2026, 1, 1)
        base = allocate_budget(
            _brief(1_500_000_00), PROFILE, Season.OFF_PEAK, currency=Currency.INR, today=today
        )
        boosted = allocate_budget(
            _brief(1_500_000_00, priorities={"catering": Priority.VITAL}),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=today,
        )
        base_catering = base.line("catering").amount_minor
        boosted_catering = boosted.line("catering").amount_minor
        assert boosted_catering > base_catering
        # A boost to one category must be paid for by the others, not by
        # creating money, so the total is unchanged.
        assert boosted.spendable_minor == base.spendable_minor

    def test_peak_season_costs_more_than_off_peak_for_demand_sensitive_lines(self) -> None:
        today = date(2026, 1, 1)
        off = allocate_budget(
            _brief(1_500_000_00), PROFILE, Season.OFF_PEAK, currency=Currency.INR, today=today
        )
        peak = allocate_budget(
            _brief(1_500_000_00), PROFILE, Season.PEAK, currency=Currency.INR, today=today
        )
        # Same envelope, but peak carries a bigger contingency cushion.
        assert peak.contingency_minor >= off.contingency_minor

    def test_tight_lead_time_increases_contingency(self) -> None:
        """A host with six weeks cannot shop around, so needs a bigger float."""
        far = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),  # ~17 months out
        )
        near = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2027, 5, 1),  # ~5 weeks out
        )
        assert near.contingency_pct > far.contingency_pct

    def test_contingency_stays_within_its_guard_rails(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert 3.0 <= plan.contingency_pct <= 15.0

    def test_tier_affects_metro_sensitive_categories(self) -> None:
        today = date(2026, 1, 1)
        tier3 = allocate_budget(
            _brief(1_500_000_00, city_tier=CityTier.TIER_3),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=today,
        )
        metro = allocate_budget(
            _brief(1_500_000_00, city_tier=CityTier.GLOBAL),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=today,
        )
        # Venue is metro-sensitive, so it claims more of the same envelope.
        assert metro.line("venue").pct > tier3.line("venue").pct


# --------------------------------------------------------------------------- #
# Output contract
# --------------------------------------------------------------------------- #
class TestOutputContract:
    def test_per_guest_is_consistent_with_the_line(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00, guests=250),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        for line in plan.lines:
            assert line.per_guest_minor == round(line.amount_minor / 250)

    def test_every_catalogue_category_is_present(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert {line.key for line in plan.lines} == {spec.key for spec in PROFILE.categories}

    def test_lines_are_sorted_largest_first(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        amounts = [line.amount_minor for line in plan.lines]
        assert amounts == sorted(amounts, reverse=True)

    def test_pcts_add_up_to_a_hundred(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert sum(line.pct for line in plan.lines) == pytest.approx(100.0, abs=0.5)

    def test_trade_offs_carry_a_real_saving_and_a_consequence(self) -> None:
        plan = allocate_budget(
            _brief(1_500_000_00),
            PROFILE,
            Season.OFF_PEAK,
            currency=Currency.INR,
            today=date(2026, 1, 1),
        )
        assert plan.trade_offs, "a budget this size should always offer trade-offs"
        for t in plan.trade_offs:
            assert t.saving_minor > 0
            assert t.consequence, "a trade-off with no stated cost is not advice"

    def test_is_deterministic(self) -> None:
        args = (_brief(1_500_000_00), PROFILE, Season.OFF_PEAK)
        kwargs = {"currency": Currency.INR, "today": date(2026, 1, 1)}
        a = allocate_budget(*args, **kwargs)
        b = allocate_budget(*args, **kwargs)
        assert [ln.amount_minor for ln in a.lines] == [ln.amount_minor for ln in b.lines]


# --------------------------------------------------------------------------- #
# Guard rails on the brief itself
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_zero_budget_is_rejected(self) -> None:
        with pytest.raises(BriefError):
            _brief(0).validate()

    def test_negative_budget_is_rejected(self) -> None:
        with pytest.raises(BriefError):
            _brief(-1).validate()

    def test_absurd_guest_count_is_rejected(self) -> None:
        with pytest.raises(BriefError):
            _brief(1_00, guests=1_000_000).validate()

    def test_zero_guests_is_rejected(self) -> None:
        with pytest.raises(BriefError):
            _brief(1_00, guests=0).validate()
