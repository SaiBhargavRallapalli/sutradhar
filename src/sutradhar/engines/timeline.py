"""Timeline engine - critical-path scheduling backwards from the event date.

Event work is naturally *anchored to the event*, not to today: "send
invitations twelve weeks out", "confirm vendors three weeks out". So the
scheduler runs the classic CPM passes on a relative-day axis where day ``0`` is
the event, then converts the result into real dates.

Two questions get answered rigorously, and both are the ones a stressed host
actually asks:

* **"How early do I have to start?"** - the forward pass yields
  ``min_required_lead_days``; compare it with the days actually available and you
  know instantly whether the plan is achievable.
* **"What can slip without hurting the day?"** - the backward pass gives each
  task its *slack* against the deadline the catalogue gives it. Zero slack means
  the task is already at its latest acceptable start. Separately, the engine
  reports the *critical path*: the longest chain of dependent work, which is
  what sets the minimum total preparation time.

When the schedule does not fit, the engine does not merely complain - it
*compresses*, shortening durations along the late chain until the plan fits, and
reports exactly which tasks were squeezed so the host can decide whether to
accept that or move the date.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import date

from ..catalog.specs import EventTypeProfile, TaskSpec
from ..enums import RiskLevel, Season
from ..models import Brief, PlanWarning, RunOfShowSlot, Task, TimelinePlan
from .context import CostContext, cost_context

__all__ = ["build_timeline", "critical_path", "topological_order"]

#: Within this many days of the ideal runway a plan is feasible but worth
#: flagging as tight.
_TIGHT_RUNWAY_DAYS = 60


# --------------------------------------------------------------------------- #
# Graph helpers
# --------------------------------------------------------------------------- #
def topological_order(specs: tuple[TaskSpec, ...]) -> list[TaskSpec]:
    """Return ``specs`` in dependency order, or raise ``ValueError``.

    Detects both unknown dependency ids and cycles, and names the offending
    tasks so a catalogue author can fix the entry immediately.
    """
    by_id = {spec.id: spec for spec in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in by_id:
                raise ValueError(f"task {spec.id!r} depends on unknown task {dep!r}")

    indegree = {spec.id: 0 for spec in specs}
    children: dict[str, list[str]] = {spec.id: [] for spec in specs}
    for spec in specs:
        for dep in spec.depends_on:
            indegree[spec.id] += 1
            children[dep].append(spec.id)

    # A sorted ready-set keeps the output deterministic for identical inputs.
    ready = deque(sorted(tid for tid, deg in indegree.items() if deg == 0))
    order: list[TaskSpec] = []
    while ready:
        current = ready.popleft()
        order.append(by_id[current])
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)

    if len(order) != len(specs):
        stuck = sorted(tid for tid, deg in indegree.items() if deg > 0)
        raise ValueError(f"task dependencies form a cycle involving: {', '.join(stuck)}")
    return order


def critical_path(
    order: list[TaskSpec],
    duration: dict[str, int],
) -> list[str]:
    """Return the longest chain of dependent tasks by total duration.

    This is the textbook critical path: the sequence of work that sets the
    minimum total preparation time. Computed with a single forward dynamic
    program over the topological order - ``O(V + E)`` - rather than a full CPM
    re-run, and deterministic on ties so the same brief always yields the same
    path.

    Note this is deliberately distinct from a task's ``slack_days``. Slack is
    measured against the *deadline* the catalogue gives each task ("confirm
    vendors three weeks out"), so a well-spaced plan can have slack everywhere
    while still having a longest chain. Both views are useful: the path says
    what must flow in order; slack says what can safely slip.
    """
    best: dict[str, int] = {spec.id: duration[spec.id] for spec in order}
    previous: dict[str, str] = {}

    for spec in order:
        for dep in spec.depends_on:
            candidate = best[dep] + duration[spec.id]
            if candidate > best[spec.id]:
                best[spec.id] = candidate
                previous[spec.id] = dep

    if not best:
        return []

    end: str | None = max(best, key=lambda tid: (best[tid], tid))
    path: list[str] = []
    while end is not None:
        path.append(end)
        end = previous.get(end)
    path.reverse()
    return path


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
class _Scheduler:
    """Holds the mutable duration table while CPM is iterated during compression.

    Durations are the only thing compression changes, so they live in a table
    separate from the immutable catalogue specs.
    """

    def __init__(self, specs: tuple[TaskSpec, ...]) -> None:
        self.specs = specs
        self.order = topological_order(specs)
        self.duration: dict[str, int] = {spec.id: spec.duration_days for spec in specs}
        self.compressed: set[str] = set()
        self._successors: dict[str, list[str]] = {spec.id: [] for spec in specs}
        for spec in specs:
            for dep in spec.depends_on:
                self._successors[dep].append(spec.id)

    def run(self) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
        """Execute the forward and backward passes.

        Returns ``(earliest_start, earliest_finish, latest_start, latest_finish)``
        on the relative-day axis where day 0 is the event date.
        """
        es: dict[str, int] = {}
        ef: dict[str, int] = {}
        for spec in self.order:
            if spec.depends_on:
                start = max(ef[dep] for dep in spec.depends_on)
            else:
                start = spec.offset_days - self.duration[spec.id]
            es[spec.id] = start
            ef[spec.id] = start + self.duration[spec.id]

        lf: dict[str, int] = {}
        ls: dict[str, int] = {}
        for spec in reversed(self.order):
            deadline = spec.offset_days
            if self._successors[spec.id]:
                deadline = min([deadline, *[ls[child] for child in self._successors[spec.id]]])
            lf[spec.id] = deadline
            ls[spec.id] = deadline - self.duration[spec.id]

        return es, ef, ls, lf

    def slack(self, es: dict[str, int], ls: dict[str, int]) -> dict[str, int]:
        return {tid: ls[tid] - es[tid] for tid in es}


def build_timeline(
    brief: Brief,
    profile: EventTypeProfile,
    season: Season,
    *,
    today: date | None = None,
) -> TimelinePlan:
    """Schedule ``profile``'s tasks for ``brief`` and validate the lead time."""
    ctx = cost_context(brief, season, today=today or date.today())
    return _schedule(profile, ctx)


def _schedule(profile: EventTypeProfile, ctx: CostContext) -> TimelinePlan:
    event_date = ctx.event_date
    scheduler = _Scheduler(profile.tasks)
    _es0, _ef0, _ls0, _lf0 = scheduler.run()
    ideal_required = -min(_es0.values())

    # The dependency floor: the least time the work itself can take, ignoring
    # the catalogue's ideal spread. This is the real "can this be done at all?"
    # threshold - a plan can be compacted to it, but never below it.
    floor = sum(
        scheduler.duration[task_id]
        for task_id in critical_path(scheduler.order, scheduler.duration)
    )

    compacted_from: int | None = None
    if ctx.lead_days < ideal_required and ctx.lead_days > floor > 0:
        # Pull every deadline forward proportionally so the schedule fits the
        # window that actually exists, while dependencies keep the order intact.
        scale = ctx.lead_days / ideal_required
        scheduler = _Scheduler(
            tuple(
                replace(spec, offset_days=round(spec.offset_days * scale)) for spec in profile.tasks
            )
        )
        compacted_from = ideal_required

    es, _ef, ls, _lf = scheduler.run()

    # Compression budget: never loop more times than there is duration to remove.
    guard = sum(spec.duration_days for spec in profile.tasks)
    while guard > 0:
        slack = scheduler.slack(es, ls)
        required_lead = -min(es.values())
        late = min(slack.values())
        if late >= 0 and ctx.lead_days >= required_lead:
            break
        guard -= 1

        target = _compression_target(scheduler, slack, es, late)
        if target is None:
            break
        scheduler.duration[target] -= 1
        scheduler.compressed.add(target)
        es, _ef, ls, _lf = scheduler.run()

    slack = scheduler.slack(es, ls)
    required_lead = -min(es.values())
    is_feasible = min(slack.values()) >= 0 and ctx.lead_days >= max(required_lead, floor)
    path = critical_path(scheduler.order, scheduler.duration)
    path_set = set(path)

    tasks = tuple(
        Task(
            id=spec.id,
            title=spec.title,
            offset_days=spec.offset_days,
            due=date.fromordinal(event_date.toordinal() + spec.offset_days),
            owner=spec.owner,
            phase=spec.phase,
            description=spec.description,
            duration_days=scheduler.duration[spec.id],
            depends_on=spec.depends_on,
            slack_days=slack[spec.id],
            earliest_start=date.fromordinal(event_date.toordinal() + es[spec.id]),
            latest_start=date.fromordinal(event_date.toordinal() + ls[spec.id]),
            critical=spec.id in path_set,
        )
        for spec in scheduler.order
    )

    warnings = _warnings(
        ctx,
        profile,
        is_feasible,
        floor,
        ideal_required,
        scheduler.compressed,
        compacted_from=compacted_from,
    )

    return TimelinePlan(
        event_date=event_date,
        lead_days=ctx.lead_days,
        min_required_lead_days=floor,
        feasible=is_feasible,
        tasks=tasks,
        critical_path=tuple(path),
        run_of_show=tuple(
            RunOfShowSlot(
                start=slot.start,
                end=slot.end,
                title=slot.title,
                owner=slot.owner,
                notes=slot.notes,
                day_offset=slot.day_offset,
            )
            for slot in profile.run_of_show
        ),
        season=ctx.season,
        warnings=tuple(warnings),
    )


def _compression_target(
    scheduler: _Scheduler,
    slack: dict[str, int],
    es: dict[str, int],
    late: int,
) -> str | None:
    """Pick the single most valuable duration day to give up.

    Shortening a task helps twice over: it pulls its successors earlier and
    pushes its predecessors later, so the most-late task on the chain is always
    the right place to start. When nothing is late but the lead time is short,
    compressing the earliest-starting task cuts the required runway.
    """
    compressible = [tid for tid in scheduler.duration if scheduler.duration[tid] > 1]
    if not compressible:
        return None
    if late < 0:
        return min(compressible, key=lambda tid: (slack[tid], tid))
    return min(compressible, key=lambda tid: (es[tid], tid))


def _warnings(
    ctx: CostContext,
    profile: EventTypeProfile,
    is_feasible: bool,
    floor: int,
    ideal_required: int,
    compressed: set[str],
    *,
    compacted_from: int | None = None,
) -> list[PlanWarning]:
    out: list[PlanWarning] = []

    if not is_feasible:
        out.append(
            PlanWarning(
                code="infeasible_schedule",
                message=(
                    f"The work itself needs at least {floor} days of preparation, and the ideal "
                    f"plan assumes {ideal_required}, but only {max(ctx.lead_days, 0)} remain."
                ),
                severity=RiskLevel.CRITICAL,
                section="timeline",
                suggestion="Move the date later, trim the guest list, or drop optional functions.",
            )
        )
    elif compacted_from is not None:
        out.append(
            PlanWarning(
                code="schedule_compacted",
                message=(
                    f"Only {ctx.lead_days} days remain against an ideal {compacted_from}-day runway, "
                    "so every deadline has been pulled forward proportionally."
                ),
                severity=RiskLevel.HIGH,
                section="timeline",
                suggestion="Book the venue and the top vendors immediately - the buffer is now gone.",
            )
        )

    if compressed:
        names = ", ".join(sorted(compressed)[:4])
        out.append(
            PlanWarning(
                code="compressed_tasks",
                message=(
                    f"{len(compressed)} task(s) were shortened to fit the time available, "
                    f"starting with {names}."
                ),
                severity=RiskLevel.HIGH,
                section="timeline",
                suggestion="Review each compressed task - some work cannot realistically be rushed.",
            )
        )

    ideal_gap = profile.ideal_lead_days - ctx.lead_days
    if is_feasible and compacted_from is None and 0 < ideal_gap <= _TIGHT_RUNWAY_DAYS:
        out.append(
            PlanWarning(
                code="tight_but_feasible",
                message=(
                    f"Feasible, but {ideal_gap} days short of the ideal "
                    f"{profile.ideal_lead_days}-day runway for this event type."
                ),
                severity=RiskLevel.MEDIUM,
                section="timeline",
                suggestion="Book the venue and the top vendors this week - they are what disappears first.",
            )
        )

    if ctx.season is Season.PEAK:
        out.append(
            PlanWarning(
                code="peak_booking_pressure",
                message="Peak season: the best vendors and venues for this date are usually locked first.",
                severity=RiskLevel.MEDIUM,
                section="timeline",
                suggestion="Ask for cancellation terms as well as price - flexibility is worth more at peak.",
            )
        )

    return out
