"""End-to-end pipeline tests.

These exercise the whole system the way a host or an integrator does: hand in
a :class:`Brief`, get back a complete :class:`Plan`. The assertions are about
*invariants that must hold for every plan*, because those are what make the
output trustworthy.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from sutradhar import Brief, CityTier, Currency, EventType, Season, plan_event
from sutradhar.config import Settings
from sutradhar.exceptions import BriefError
from sutradhar.models import Plan, TasteProfile

EVENT_TYPES = [
    EventType.WEDDING,
    EventType.BIRTHDAY,
    EventType.COMMUNITY,
    EventType.HOUSEWARMING,
    EventType.ANNIVERSARY,
]


def _brief(et: EventType = EventType.WEDDING, **kwargs) -> Brief:
    defaults = {
        "event_type": et,
        "event_date": date(2027, 6, 6),
        "guest_count": 150,
        "budget_total_minor": 8_00_000_00,
        "currency": Currency.INR,
        "city_tier": CityTier.TIER_2,
        "location": "Pune",
    }
    defaults.update(kwargs)
    return Brief(**defaults)


class TestInvariants:
    """Properties that must hold for *any* brief that validates."""

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_budget_reconciles_exactly(self, et: EventType, offline_settings: Settings) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        assert plan.budget.unallocated_minor == 0
        assert sum(line.amount_minor for line in plan.budget.lines) == plan.budget.spendable_minor

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_every_section_is_populated(self, et: EventType, offline_settings: Settings) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        assert plan.budget.lines
        assert plan.timeline.tasks
        assert plan.vendors.items
        assert plan.risks.items
        assert plan.ideas

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_vendor_book_by_dates_precede_the_event(
        self, et: EventType, offline_settings: Settings
    ) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        for v in plan.vendors.items:
            assert v.book_by <= plan.brief.event_date, f"{v.label} booked after the event"

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_payment_milestones_land_on_or_before_the_event(
        self, et: EventType, offline_settings: Settings
    ) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        for m in plan.vendors.milestones:
            assert m.due <= plan.brief.event_date

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_risks_have_a_mitigation_and_a_bounded_score(
        self, et: EventType, offline_settings: Settings
    ) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        for r in plan.risks.items:
            assert r.mitigation, f"risk {r.key} has no mitigation"
            assert 1 <= r.likelihood <= 5
            assert 1 <= r.impact <= 5

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_every_idea_is_grounded(self, et: EventType, offline_settings: Settings) -> None:
        """An idea that does not fit the budget must never be presented as if it did."""
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        for idea in plan.ideas:
            assert idea.grounded, f"{idea.name} is not grounded in the budget"

    @pytest.mark.parametrize("et", EVENT_TYPES)
    def test_provenance_is_recorded_for_every_section(
        self, et: EventType, offline_settings: Settings
    ) -> None:
        plan = plan_event(_brief(et), settings=offline_settings, today=date(2026, 1, 1))
        assert set(plan.provenance) == {"budget", "timeline", "vendors", "risks", "ideas"}


class TestDeterminism:
    def test_identical_input_yields_identical_output(self, offline_settings: Settings) -> None:
        """Ignoring the timestamp, two runs on the same day are byte-identical."""
        a = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        b = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        da, db = a.to_dict(), b.to_dict()
        da.pop("generated_at"), db.pop("generated_at")
        assert da == db

    def test_the_reference_date_drives_the_whole_plan(self, offline_settings: Settings) -> None:
        """`today` is injected, never read from the clock, so a plan scheduled
        for the same event but generated later is different in the right way."""
        early = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        late = plan_event(_brief(), settings=offline_settings, today=date(2027, 5, 1))
        assert early.timeline.lead_days > late.timeline.lead_days

    def test_offline_runs_never_touch_the_network(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        assert plan.ai_used is False
        assert plan.ai_provider in {"catalogue", "mock", "none"}


class TestPersonalisation:
    def test_taste_shows_up_in_the_chosen_ideas(self, offline_settings: Settings) -> None:
        plain = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        styled = plan_event(
            _brief(
                taste=TasteProfile(
                    vibe="minimal modern", style_keywords=("minimal", "modern", "monochrome")
                )
            ),
            settings=offline_settings,
            today=date(2026, 1, 1),
        )
        plain_names = [i.name for i in plain.ideas]
        styled_names = [i.name for i in styled.ideas]
        assert plain_names != styled_names, "taste should change which ideas are offered"

    def test_avoid_list_can_suppress_a_theme(self, offline_settings: Settings) -> None:
        plan = plan_event(
            _brief(taste=TasteProfile(avoid=("traditional", "temple", "brass"))),
            settings=offline_settings,
            today=date(2026, 1, 1),
        )
        for idea in plan.ideas:
            assert "temple" not in idea.name.lower()

    def test_ideas_report_a_cost_direction(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        for idea in plan.ideas:
            assert isinstance(idea.estimated_delta_pct, float)
            assert idea.cost_fit, f"{idea.name} does not explain its cost impact"


class TestValidation:
    def test_invalid_brief_raises_before_any_engine_runs(self, offline_settings: Settings) -> None:
        with pytest.raises(BriefError):
            plan_event(_brief(guest_count=0), settings=offline_settings, today=date(2026, 1, 1))

    def test_zero_budget_raises(self, offline_settings: Settings) -> None:
        with pytest.raises(BriefError):
            plan_event(
                _brief(budget_total_minor=0), settings=offline_settings, today=date(2026, 1, 1)
            )

    def test_past_date_still_plans_but_reports_lead_time(self, offline_settings: Settings) -> None:
        """A date in the past is a planning mistake, not a crash."""
        plan = plan_event(
            _brief(event_date=date(2020, 1, 1)), settings=offline_settings, today=date(2026, 1, 1)
        )
        assert plan.timeline.lead_days < 0
        assert plan.timeline.feasible is False


class TestBudgetScaling:
    @pytest.mark.parametrize("guests", [30, 100, 500, 1500])
    def test_plan_survives_extreme_guest_counts(
        self, guests: int, offline_settings: Settings
    ) -> None:
        plan = plan_event(
            _brief(guest_count=guests), settings=offline_settings, today=date(2026, 1, 1)
        )
        assert plan.budget.unallocated_minor == 0

    @pytest.mark.parametrize("total", [1_00_000, 10_00_000, 1_00_00_000, 10_00_00_000])
    def test_plan_survives_extreme_budgets(self, total: int, offline_settings: Settings) -> None:
        plan = plan_event(
            _brief(budget_total_minor=total), settings=offline_settings, today=date(2026, 1, 1)
        )
        assert plan.budget.unallocated_minor == 0
        assert all(line.amount_minor >= 0 for line in plan.budget.lines)

    def test_a_tiny_budget_does_not_produce_negative_lines(
        self, offline_settings: Settings
    ) -> None:
        plan = plan_event(
            _brief(guest_count=500, budget_total_minor=10_00),
            settings=offline_settings,
            today=date(2026, 1, 1),
        )
        assert all(line.amount_minor >= 0 for line in plan.budget.lines)


class TestSerialisation:
    def test_plan_round_trips_through_json(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        data = json.loads(plan.to_json())
        assert data["brief"]["guest_count"] == 150

    def test_derived_values_are_present_in_the_json(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        data = plan.to_dict()
        assert data["budget"]["unallocated_minor"] == 0
        assert "contingency_pct" in data["budget"]
        assert "score" in data["risks"]["items"][0]
        assert "all_warnings" in data

    def test_no_datetime_object_leaks_into_the_json(self, offline_settings: Settings) -> None:
        """Dates must serialise as ISO strings, never as Python reprs."""
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        text = plan.to_json()
        assert "datetime.date(" not in text
        assert "datetime.datetime(" not in text

    def test_json_is_unicode_clean(self, offline_settings: Settings) -> None:
        plan = plan_event(
            _brief(title="Aarav & Meera"), settings=offline_settings, today=date(2026, 1, 1)
        )
        json.loads(plan.to_json())  # must not raise

    def test_toplevel_plan_is_a_model(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        assert isinstance(plan, Plan)


class TestSeasonHandling:
    def test_explicit_season_override_is_honoured(self, offline_settings: Settings) -> None:
        off = plan_event(
            _brief(season_override=Season.OFF_PEAK),
            settings=offline_settings,
            today=date(2026, 1, 1),
        )
        peak = plan_event(
            _brief(season_override=Season.PEAK), settings=offline_settings, today=date(2026, 1, 1)
        )
        assert off.timeline.season is Season.OFF_PEAK
        assert peak.timeline.season is Season.PEAK

    def test_season_is_recorded_on_the_timeline(self, offline_settings: Settings) -> None:
        plan = plan_event(_brief(), settings=offline_settings, today=date(2026, 1, 1))
        assert isinstance(plan.timeline.season, Season)
