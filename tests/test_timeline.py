"""Timeline engine tests.

Three properties matter here and each is a promise the engine makes:

* **Soundness of the graph** - dependency order is respected, cycles and
  dangling references are rejected by name.
* **Correctness of CPM** - the critical path really is the longest chain, and
  slack is computed against deadlines rather than guessed.
* **Honesty about feasibility** - a cramped plan says so, and says by how
  much; a compacted plan reports that it compacted.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from sutradhar import Brief, CityTier, Currency, EventType
from sutradhar.catalog.registry import get_profile
from sutradhar.catalog.specs import TaskSpec
from sutradhar.engines.timeline import build_timeline, critical_path, topological_order
from sutradhar.enums import OwnerRole, Season

WEDDING = get_profile(EventType.WEDDING)


def _brief(days_out: int, **kwargs) -> Brief:
    today = date(2026, 1, 1)
    return Brief(
        event_type=EventType.WEDDING,
        event_date=date.fromordinal(today.toordinal() + days_out),
        guest_count=kwargs.pop("guest_count", 200),
        budget_total_minor=kwargs.pop("budget_total_minor", 1_500_000_00),
        currency=Currency.INR,
        city_tier=CityTier.TIER_2,
        **kwargs,
    )


TODAY = date(2026, 1, 1)


def _spec(id: str, *, deps: tuple[str, ...] = (), offset: int = -10, duration: int = 1) -> TaskSpec:
    return TaskSpec(
        id=id,
        title=id.title(),
        offset_days=offset,
        duration_days=duration,
        owner=OwnerRole.HOST,
        depends_on=deps,
    )


# --------------------------------------------------------------------------- #
# Graph helpers
# --------------------------------------------------------------------------- #
class TestTopologicalOrder:
    def test_respects_dependencies(self) -> None:
        specs = (_spec("c", deps=("a", "b")), _spec("a"), _spec("b", deps=("a",)))
        order = [s.id for s in topological_order(specs)]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_rejects_unknown_dependency_by_name(self) -> None:
        with pytest.raises(ValueError) as exc:
            topological_order((_spec("a", deps=("ghost",)),))
        assert "ghost" in str(exc.value)

    def test_rejects_a_cycle(self) -> None:
        specs = (_spec("a", deps=("b",)), _spec("b", deps=("a",)))
        with pytest.raises(ValueError) as exc:
            topological_order(specs)
        assert "cycle" in str(exc.value)

    def test_rejects_a_self_cycle(self) -> None:
        with pytest.raises(ValueError):
            topological_order((_spec("a", deps=("a",)),))

    def test_is_deterministic(self) -> None:
        specs = tuple(_spec(c) for c in "dcba")
        assert [s.id for s in topological_order(specs)] == [s.id for s in topological_order(specs)]


class TestCriticalPath:
    def test_picks_the_longest_chain_not_the_first(self) -> None:
        # Two routes to `end`: a(1)->b(1) = 2, and c(10) = 10.
        specs = (
            _spec("a", duration=1),
            _spec("b", deps=("a",), duration=1),
            _spec("c", duration=10),
            _spec("end", deps=("b", "c"), duration=1),
        )
        durations = {s.id: s.duration_days for s in specs}
        path = critical_path(topological_order(specs), durations)
        assert path == ["c", "end"]

    def test_path_is_a_valid_dependency_chain(self) -> None:
        order = topological_order(WEDDING.tasks)
        durations = {s.id: s.duration_days for s in WEDDING.tasks}
        path = critical_path(order, durations)
        by_id = {s.id: s for s in WEDDING.tasks}
        # Every step after the first must actually depend on the one before it.
        for prev, curr in itertools.pairwise(path):
            assert prev in by_id[curr].depends_on, f"{curr} does not depend on {prev}"

    def test_path_length_equals_min_required_lead_for_the_wedding(self) -> None:
        """The dependency floor and the longest chain are the same quantity."""
        durations = {s.id: s.duration_days for s in WEDDING.tasks}
        path = critical_path(topological_order(WEDDING.tasks), durations)
        floor = sum(durations[tid] for tid in path)
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert floor == plan.min_required_lead_days

    def test_empty_graph(self) -> None:
        assert critical_path([], {}) == []

    def test_single_node(self) -> None:
        specs = (_spec("only", duration=3),)
        assert critical_path(topological_order(specs), {"only": 3}) == ["only"]

    def test_deterministic_on_ties(self) -> None:
        # Two equal-length routes: the tie must break the same way every time.
        specs = (
            _spec("a", duration=5),
            _spec("b", duration=5),
            _spec("end", deps=("a", "b"), duration=1),
        )
        durations = {s.id: s.duration_days for s in specs}
        order = topological_order(specs)
        first = critical_path(order, durations)
        for _ in range(10):
            assert critical_path(order, durations) == first


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
class TestScheduling:
    def test_tasks_are_listed_in_dependency_order(self) -> None:
        """Tasks are emitted topologically, not by date, so a reader can follow
        the chain without cross-referencing. Due dates still advance overall."""
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        position = {t.id: i for i, t in enumerate(plan.tasks)}
        for task in plan.tasks:
            for dep in task.depends_on:
                assert position[dep] < position[task.id]

    def test_the_first_task_has_no_dependencies(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert plan.tasks[0].depends_on == ()

    def test_every_task_lands_on_or_before_the_event_for_prep_work(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        for task in plan.tasks:
            if task.offset_days <= 0:
                assert task.due <= plan.event_date

    def test_due_dates_are_derived_from_the_event_date(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        for task in plan.tasks:
            expected = date.fromordinal(plan.event_date.toordinal() + task.offset_days)
            assert task.due == expected

    def test_dependency_order_is_respected_in_dates(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        by_id = {t.id: t for t in plan.tasks}
        for task in plan.tasks:
            for dep in task.depends_on:
                # A successor cannot finish before its predecessor finishes.
                assert by_id[dep].due <= task.due, f"{task.id} due before its dependency {dep}"

    def test_critical_path_tasks_are_flagged_critical(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        flagged = {t.id for t in plan.tasks if t.critical}
        assert set(plan.critical_path) <= flagged

    def test_slack_is_never_negative_on_a_feasible_plan(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        if plan.feasible:
            assert all(t.slack_days >= 0 for t in plan.tasks)

    def test_critical_load_is_the_sum_of_critical_durations(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        expected = sum(t.duration_days for t in plan.tasks if t.critical)
        assert plan.critical_load_days == expected


# --------------------------------------------------------------------------- #
# Feasibility, compaction and compression
# --------------------------------------------------------------------------- #
class TestFeasibility:
    def test_a_generous_window_is_feasible(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert plan.feasible is True
        assert plan.lead_days == 400

    def test_an_impossible_window_is_reported_not_hidden(self) -> None:
        # Far below the ~54-day dependency floor.
        plan = build_timeline(_brief(20), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert plan.feasible is False
        assert plan.lead_days < plan.min_required_lead_days

    def test_infeasible_plan_carries_a_warning(self) -> None:
        plan = build_timeline(_brief(20), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert any(
            "lead" in w.message.lower() or "days" in w.message.lower() for w in plan.warnings
        ), "an infeasible plan must say so"

    def test_a_tight_but_possible_window_compacts_rather_than_fails(self) -> None:
        # Comfortably above the floor, but short of the ideal 333-day spread.
        plan = build_timeline(_brief(120), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert plan.min_required_lead_days <= 120
        assert any(
            w.code in {"schedule_compacted", "compressed_tasks", "tight_but_feasible"}
            for w in plan.warnings
        ), "a squeezed schedule should be reported, not applied silently"

    def test_compaction_keeps_dependency_order(self) -> None:
        plan = build_timeline(_brief(120), WEDDING, Season.OFF_PEAK, today=TODAY)
        by_id = {t.id: t for t in plan.tasks}
        for task in plan.tasks:
            for dep in task.depends_on:
                assert by_id[dep].due <= task.due

    def test_compression_reports_which_tasks_were_squeezed(self) -> None:
        plan = build_timeline(_brief(120), WEDDING, Season.OFF_PEAK, today=TODAY)
        compressed = [
            w
            for w in plan.warnings
            if "shorten" in w.message.lower() or "compress" in w.message.lower()
        ]
        for w in compressed:
            assert w.suggestion, "a compression warning must tell the host what to do"

    def test_lead_days_matches_the_brief(self) -> None:
        for days in (30, 90, 200, 400, 700):
            plan = build_timeline(_brief(days), WEDDING, Season.OFF_PEAK, today=TODAY)
            assert plan.lead_days == days


# --------------------------------------------------------------------------- #
# Determinism and catalogue integrity
# --------------------------------------------------------------------------- #
class TestDeterminism:
    def test_same_brief_same_timeline(self) -> None:
        a = build_timeline(_brief(300), WEDDING, Season.OFF_PEAK, today=TODAY)
        b = build_timeline(_brief(300), WEDDING, Season.OFF_PEAK, today=TODAY)
        assert [(t.id, t.due, t.slack_days, t.critical) for t in a.tasks] == [
            (t.id, t.due, t.slack_days, t.critical) for t in b.tasks
        ]

    @pytest.mark.parametrize(
        "et",
        [
            EventType.WEDDING,
            EventType.BIRTHDAY,
            EventType.COMMUNITY,
            EventType.HOUSEWARMING,
            EventType.ANNIVERSARY,
        ],
    )
    def test_every_catalogue_schedules_cleanly(self, et: EventType) -> None:
        profile = get_profile(et)
        brief = Brief(
            event_type=et,
            event_date=date(2027, 6, 6),
            guest_count=100,
            budget_total_minor=5_00_000_00,
            currency=Currency.INR,
        )
        plan = build_timeline(brief, profile, Season.OFF_PEAK, today=TODAY)
        assert len(plan.tasks) == len(profile.tasks)
        assert plan.min_required_lead_days > 0

    def test_every_catalogue_task_ids_are_unique(self) -> None:
        ids = [s.id for s in WEDDING.tasks]
        assert len(ids) == len(set(ids))

    def test_run_of_show_slots_are_chronological(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        keyed = [(s.day_offset, s.start, s.end) for s in plan.run_of_show]
        assert keyed == sorted(keyed)

    def test_run_of_show_slots_are_well_formed(self) -> None:
        plan = build_timeline(_brief(400), WEDDING, Season.OFF_PEAK, today=TODAY)
        for slot in plan.run_of_show:
            assert slot.start < slot.end, f"{slot.title} ends before it starts"
