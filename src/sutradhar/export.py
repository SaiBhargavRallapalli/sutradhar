"""Export a plan into formats people actually use.

Four renderers, all standard-library only:

* **Markdown** - the readable plan document, suitable for a repo, a wiki or an
  email to family.
* **CSV** - budget and timeline sheets that open in any spreadsheet, so a host
  can track real spend against the plan.
* **iCalendar (ICS)** - every task as an all-day event, importable into Google
  Calendar, Apple Calendar and Outlook. This is usually the single most useful
  export, because it is where the plan stops being a document and starts being
  a schedule.

The iCalendar writer follows RFC 5545: CRLF line endings, escaped text values
and folded long lines.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Plan
from .money import format_minor

__all__ = [
    "budget_csv",
    "timeline_csv",
    "to_ics",
    "to_markdown",
    "vendor_csv",
    "write_plan",
]

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _symbol(plan: Plan) -> str:
    return plan.budget.currency.info.symbol


def _money(minor: int, plan: Plan) -> str:
    return format_minor(minor, plan.budget.currency)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def to_markdown(plan: Plan) -> str:
    """Render the whole plan as a Markdown document."""
    brief = plan.brief
    budget = plan.budget
    timeline = plan.timeline
    _symbol(plan)

    out: list[str] = []
    out.append(f"# {brief.display_title}")
    out.append("")
    out.append(
        f"**{brief.event_type.value.title()}** on {brief.event_date:%A, %d %B %Y}"
        f"{' in ' + brief.location if brief.location else ''} - "
        f"{brief.guest_count:,} guests."
    )
    out.append("")

    # -- At a glance ------------------------------------------------------- #
    out.append("## At a glance")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| Total budget | {_money(budget.total_minor, plan)} |")
    out.append(
        f"| Reserved as contingency ({budget.contingency_pct:.0f}%) | {_money(budget.contingency_minor, plan)} |"
    )
    out.append(f"| Cost per guest | {_money(budget.per_guest_minor, plan)} |")
    out.append(
        f"| Lead time | {timeline.lead_days} days available, "
        f"{'✅ achievable' if timeline.feasible else '⚠️ needs attention'} |"
    )
    out.append(f"| Critical path | {len(timeline.critical_path)} linked tasks |")
    out.append(
        f"| Ideas source | {'language model' if plan.ai_used else 'curated catalogue'} "
        f"({plan.ai_provider}) |"
    )
    out.append("")

    # -- Warnings ---------------------------------------------------------- #
    warnings = sorted(
        plan.all_warnings,
        key=lambda w: (_SEVERITY_ORDER.get(w.severity.value, 9), w.section, w.code),
    )
    if warnings:
        out.append("## What to watch")
        out.append("")
        for warning in warnings:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}.get(
                warning.severity.value, "•"
            )
            out.append(f"- {icon} **{warning.message}**")
            if warning.suggestion:
                out.append(f"  - _{warning.suggestion}_")
        out.append("")

    # -- Budget ------------------------------------------------------------ #
    out.append("## Budget")
    out.append("")
    out.append("| Category | Amount | % of spendable | Per guest | Why |")
    out.append("|---|---:|---:|---:|---|")
    for line in budget.lines:
        out.append(
            f"| {line.label} | {_money(line.amount_minor, plan)} | {line.pct:.1f}% "
            f"| {_money(line.per_guest_minor, plan)} | {line.rationale} |"
        )
    out.append(
        f"| **Contingency** | **{_money(budget.contingency_minor, plan)}** "
        f"| {budget.contingency_pct:.1f}% | | Unplanned extras and price rises |"
    )
    out.append("")

    if budget.trade_offs:
        out.append("### Trade-offs if you need to free up cash")
        out.append("")
        for trade in budget.trade_offs:
            out.append(
                f"- **Save {_money(trade.saving_minor, plan)}** by trimming *{trade.category_label}* "
                f"(impact: {trade.impact.value}). {trade.consequence}"
            )
            for step in trade.steps:
                out.append(f"  - {step}")
        out.append("")

    # -- Timeline ---------------------------------------------------------- #
    out.append("## Timeline")
    out.append("")
    if timeline.critical_path:
        out.append(
            "**Critical path (do these first, in order):** " + " → ".join(timeline.critical_path)
        )
        out.append("")

    out.append("| Due | Task | Owner | Phase | Days | Slack |")
    out.append("|---|---|---|---|---:|---:|")
    for task in timeline.tasks:
        marker = " 🔴" if task.critical else ""
        slack = "0 ⚡" if task.slack_days == 0 else str(task.slack_days)
        out.append(
            f"| {task.due:%d %b %Y} | {task.title}{marker} | {task.owner.value} "
            f"| {task.phase} | {task.duration_days} | {slack} |"
        )
    out.append("")

    if timeline.run_of_show:
        out.append("### Run of show")
        out.append("")
        current = None
        for slot in timeline.run_of_show:
            if slot.day_offset != current:
                current = slot.day_offset
                label = "Event day" if current == 0 else f"{abs(current)} day(s) before the event"
                out.append(f"**{label}**")
                out.append("")
                out.append("| Time | What | Who | Notes |")
                out.append("|---|---|---|---|")
            out.append(
                f"| {slot.start:%H:%M}–{slot.end:%H:%M} | {slot.title} "
                f"| {slot.owner.value} | {slot.notes} |"
            )
        out.append("")

    # -- Vendors ----------------------------------------------------------- #
    out.append("## Vendor checklist")
    out.append("")
    for item in plan.vendors.items:
        out.append(f"### {item.label} _(book by {item.book_by:%d %b %Y})_")
        out.append("")
        out.append(f"{item.why}")
        out.append("")
        out.append(f"- **Priority:** {item.priority.value}")
        out.append(f"- **Typical deposit:** {item.deposit_pct:.0f}%")
        if item.negotiation_tip:
            out.append(f"- **Negotiating:** {item.negotiation_tip}")
        if item.contract_musts:
            out.append("- **Must be in the contract:**")
            for must in item.contract_musts:
                out.append(f"  - {must}")
        if item.red_flags:
            out.append("- **Red flags:**")
            for flag in item.red_flags:
                out.append(f"  - ⚠️ {flag}")
        if item.questions:
            out.append("- **Ask them:**")
            for question in item.questions:
                out.append(f"  - {question}")
        out.append("")

    if plan.vendors.milestones:
        out.append("### Payment schedule")
        out.append("")
        out.append("| Due | Vendor | Payment | % | Amount |")
        out.append("|---|---|---|---:|---:|")
        for milestone in plan.vendors.milestones:
            out.append(
                f"| {milestone.due:%d %b %Y} | {milestone.vendor_key.title()} "
                f"| {milestone.label} | {milestone.pct:.0f}% "
                f"| {_money(milestone.amount_minor, plan)} |"
            )
        out.append("")

    # -- Risks ------------------------------------------------------------- #
    out.append("## Risk register")
    out.append("")
    out.append("| Risk | Score | Severity | Watch for | Mitigation |")
    out.append("|---|---:|---|---|---|")
    for risk in plan.risks.items:
        out.append(
            f"| {risk.title} | {risk.score} | {risk.severity.value} "
            f"| {risk.trigger_sign or '—'} | {risk.mitigation} |"
        )
    out.append("")

    # -- Ideas ------------------------------------------------------------- #
    if plan.ideas:
        out.append("## Personalised ideas")
        out.append("")
        for idea in plan.ideas:
            out.append(f"### {idea.name}")
            out.append("")
            out.append(f"_{idea.tagline}_")
            out.append("")
            if idea.palette:
                out.append(f"- **Palette:** {', '.join(idea.palette)}")
            for label, value in (
                ("Decor", idea.decor),
                ("Menu", idea.menu),
                ("Attire", idea.attire),
                ("Music", idea.music),
                ("Activities", idea.activities),
                ("Favors", idea.favors),
            ):
                if value:
                    out.append(f"- **{label}:** {value}")
            out.append(f"- **Budget fit:** {idea.cost_fit}")
            out.append(f"- **Source:** {idea.source.value}")
            out.append("")

    # -- Provenance -------------------------------------------------------- #
    out.append("---")
    out.append("")
    out.append(
        f"_Generated by Sutradhar on {plan.generated_at:%Y-%m-%d %H:%M} UTC. "
        "Budget, timeline, vendor and risk figures are computed deterministically; "
        f"{'ideas were drafted by a language model and re-grounded against the budget.' if plan.ai_used else 'ideas come from the curated catalogue.'}_"
    )
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def budget_csv(plan: Plan) -> str:
    """Budget lines as CSV, with a header row and UTF-8 BOM for Excel."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["category", "amount", "currency", "pct_of_spendable", "per_guest", "in_band", "rationale"]
    )
    for line in plan.budget.lines:
        writer.writerow(
            [
                line.label,
                f"{line.amount_minor / (10**plan.budget.currency.info.decimals):.2f}",
                plan.budget.currency.value,
                f"{line.pct:.2f}",
                f"{line.per_guest_minor / (10**plan.budget.currency.info.decimals):.2f}",
                "yes" if line.in_band else "no",
                line.rationale,
            ]
        )
    writer.writerow(
        [
            "Contingency",
            f"{plan.budget.contingency_minor / (10**plan.budget.currency.info.decimals):.2f}",
            plan.budget.currency.value,
            f"{plan.budget.contingency_pct:.2f}",
            "",
            "yes",
            "Unplanned extras and price rises",
        ]
    )
    return buffer.getvalue()


def timeline_csv(plan: Plan) -> str:
    """Timeline tasks as CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "due",
            "task",
            "owner",
            "phase",
            "duration_days",
            "slack_days",
            "critical",
            "depends_on",
            "status",
        ]
    )
    for task in plan.timeline.tasks:
        writer.writerow(
            [
                task.due.isoformat(),
                task.title,
                task.owner.value,
                task.phase,
                task.duration_days,
                task.slack_days,
                "yes" if task.critical else "no",
                " ".join(task.depends_on),
                task.status.value,
            ]
        )
    return buffer.getvalue()


def vendor_csv(plan: Plan) -> str:
    """Vendor checklist and payment schedule as one CSV."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["vendor", "priority", "book_by", "deposit_pct", "payment_label", "due", "pct", "amount"]
    )
    for item in plan.vendors.items:
        payments = [m for m in plan.vendors.milestones if m.vendor_key == item.key]
        if not payments:
            writer.writerow(
                [
                    item.label,
                    item.priority.value,
                    item.book_by.isoformat(),
                    item.deposit_pct,
                    "",
                    "",
                    "",
                    "",
                ]
            )
            continue
        for index, milestone in enumerate(payments):
            writer.writerow(
                [
                    item.label if index == 0 else "",
                    item.priority.value if index == 0 else "",
                    item.book_by.isoformat() if index == 0 else "",
                    item.deposit_pct if index == 0 else "",
                    milestone.label,
                    milestone.due.isoformat(),
                    milestone.pct,
                    f"{milestone.amount_minor / (10**plan.budget.currency.info.decimals):.2f}",
                ]
            )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# iCalendar
# --------------------------------------------------------------------------- #
def _ics_escape(text: str) -> str:
    """Escape a text value per RFC 5545 section 3.3.11."""
    return (
        str(text).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    )


def _ics_fold(line: str) -> str:
    """Fold lines longer than 75 octets, as the RFC requires."""
    if len(line) <= 75:
        return line
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


def to_ics(plan: Plan) -> str:
    """Render every task plus the event itself as an iCalendar feed."""
    stamp = (plan.generated_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sutradhar//Event Planner//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(plan.brief.display_title)}",
    ]

    def add_event(uid: str, start: date, summary: str, description: str, categories: str) -> None:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}@sutradhar",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
                f"DTEND;VALUE=DATE:{date.fromordinal(start.toordinal() + 1):%Y%m%d}",
                _ics_fold(f"SUMMARY:{_ics_escape(summary)}"),
                _ics_fold(f"DESCRIPTION:{_ics_escape(description)}"),
                _ics_fold(f"CATEGORIES:{_ics_escape(categories)}"),
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )

    add_event(
        "event",
        plan.brief.event_date,
        f"🎉 {plan.brief.display_title}",
        f"{plan.brief.event_type.value.title()} with {plan.brief.guest_count} guests.",
        "Event",
    )

    for task in plan.timeline.tasks:
        note = task.description
        if task.critical:
            note = "[CRITICAL PATH] " + note
        note += f"\nOwner: {task.owner.value}. Slack: {task.slack_days} day(s)."
        if task.depends_on:
            note += f"\nDepends on: {', '.join(task.depends_on)}"
        add_event(
            f"task-{task.id}",
            task.due,
            f"{task.title}{' 🔴' if task.critical else ''}",
            note,
            f"Task,{task.phase}",
        )

    for milestone in plan.vendors.milestones:
        add_event(
            f"pay-{milestone.vendor_key}-{milestone.due:%Y%m%d}-{milestone.pct}",
            milestone.due,
            f"💸 {milestone.vendor_key.title()}: {milestone.label}",
            f"{milestone.pct:.0f}% payment due to {milestone.vendor_key}.",
            "Payment",
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------- #
# Batch writer
# --------------------------------------------------------------------------- #
def write_plan(plan: Plan, outdir: str | Path, stem: str = "plan") -> list[Path]:
    """Write every export format into ``outdir``; return the files created."""
    target = Path(outdir)
    target.mkdir(parents=True, exist_ok=True)

    outputs = {
        f"{stem}.md": to_markdown(plan),
        f"{stem}.json": plan.to_json(),
        f"{stem}-budget.csv": budget_csv(plan),
        f"{stem}-timeline.csv": timeline_csv(plan),
        f"{stem}-vendors.csv": vendor_csv(plan),
        f"{stem}.ics": to_ics(plan),
    }

    written: list[Path] = []
    for name, content in outputs.items():
        path = target / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
