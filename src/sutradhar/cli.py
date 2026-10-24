"""Command line interface for Sutradhar.

Four verbs, each mapping to one layer of the system::

    sutradhar demo                     # end-to-end sample plan
    sutradhar plan  --brief b.json     # structured JSON brief
    sutradhar brief --text "..."       # natural-language brief
    sutradhar web   --port 8787        # zero-build accessible web UI

The CLI is a *view* over :mod:`sutradhar.pipeline`; it owns no planning logic
of its own, which keeps it trivially testable and keeps every interface (CLI,
HTTP, SDK) behaviourally identical.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from .ai import resolve_provider
from .ai.brief import try_brief_from_text
from .config import Settings, load_settings
from .enums import CityTier, EventType, Priority, WindExposure
from .export import budget_csv, timeline_csv, to_ics, to_markdown, vendor_csv, write_plan
from .inputs import brief_from_dict
from .models import Brief, Plan
from .money import Currency, format_minor
from .pipeline import plan_event
from .server.wsgi import serve
from .version import __version__

# ---------------------------------------------------------------------------
# Terminal presentation
# ---------------------------------------------------------------------------
_RESET = "\x1b[0m"
_STYLES = {
    "r": "\x1b[31m",
    "g": "\x1b[32m",
    "y": "\x1b[33m",
    "b": "\x1b[34m",
    "m": "\x1b[35m",
    "c": "\x1b[36m",
    "d": "\x1b[2m",
    "b1": "\x1b[1m",
}


class Painter:
    """Applies ANSI colour only when the terminal can actually show it."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def p(self, style: str, text: str) -> str:
        if not self.enabled or not style:
            return text
        return f"{_STYLES.get(style, '')}{text}{_RESET}"


def _term_width(default: int = 92) -> int:
    try:
        return max(60, os.get_terminal_size().columns - 2)
    except OSError:
        return default


def _rule(p: Painter, title: str = "") -> str:
    width = _term_width()
    if not title:
        return p.p("d", "-" * width)
    label = f"-- {title} "
    return p.p("d", label + "-" * max(0, width - len(label)))


def _kv(p: Painter, key: str, value: str, style: str = "") -> str:
    return "  " + p.p("d", f"{key:<15}") + " " + p.p(style, value)


def _wrap(text: str, indent: str = "  ") -> list[str]:
    width = max(40, _term_width() - len(indent))
    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent) or [
        indent
    ]


def _bar(p: Painter, fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return p.p("g", "#" * filled) + p.p("d", "." * (width - filled))


def _money(minor: int, currency: Currency) -> str:
    return format_minor(minor, currency)


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------
def render_plan(plan: Plan, p: Painter) -> list[str]:
    """Human-readable plan rendering shared by ``demo``, ``plan`` and ``brief``."""
    out: list[str] = []
    b, money = plan.brief, plan.brief.currency

    out.append("")
    out.append(_rule(p, f"{b.event_type.value.upper()} - {b.display_title}"))
    out.append("")
    out.append(_kv(p, "Event", f"{b.event_date:%A %d %B %Y}"))
    out.append(_kv(p, "Guests", f"{b.guest_count}"))
    out.append(
        _kv(p, "Where", f"{b.location or 'to be decided'} ({b.city_tier.value.replace('_', ' ')})")
    )
    out.append(_kv(p, "Setting", b.setting.value))
    if b.host_name:
        out.append(_kv(p, "Host", b.host_name))
    out.append(_kv(p, "Total budget", _money(b.budget_total_minor, money), "b1"))
    out.append("")

    # -- Budget ------------------------------------------------------------
    out.append(_rule(p, "Budget"))
    budget = plan.budget
    biggest = max((ln.amount_minor for ln in budget.lines), default=1) or 1
    for line in sorted(budget.lines, key=lambda ln: ln.amount_minor, reverse=True):
        name = line.label[:26].ljust(26)
        amount = _money(line.amount_minor, money).rjust(14)
        frac = line.amount_minor / biggest
        flag = "" if line.in_band else "  <-- outside benchmark"
        out.append(
            "  "
            + p.p("b1", name)
            + " "
            + _bar(p, frac, 18)
            + " "
            + p.p("c", amount)
            + p.p("d", f"  {line.pct:4.1f}%")
            + p.p("y", flag)
        )
    out.append("  " + p.p("d", "-" * 68))
    out.append(
        "  "
        + p.p("d", f"{'spendable'.ljust(26)}")
        + _money(budget.spendable_minor, money).rjust(24)
    )
    out.append(
        "  "
        + p.p("d", f"{'contingency float'.ljust(26)}")
        + _money(budget.contingency_minor, money).rjust(24)
        + p.p("d", f"  ({budget.contingency_pct:.1f}%)")
    )
    out.append(
        "  "
        + p.p("d", f"{'unallocated'.ljust(26)}")
        + p.p("g", _money(budget.unallocated_minor, money).rjust(24))
        + p.p("d", "  <- always 0, by construction")
    )
    if budget.lines:
        per_guest = budget.per_guest_minor
        out.append(_kv(p, "Per guest", _money(per_guest, money)))
    if budget.trade_offs:
        out.append("")
        out.append("  If you need to free up cash, in order of least harm:")
        for t in budget.trade_offs[:3]:
            saving = p.p("g", f"+{_money(t.saving_minor, money)}")
            out.append(
                f"    {saving}  {t.action} {t.category_label} {p.p('d', f'[impact: {t.impact.value}]')}"
            )
            out.extend(_wrap(t.consequence, indent="        "))
    out.append("")

    # -- Timeline ----------------------------------------------------------
    out.append(_rule(p, "Timeline"))
    tl = plan.timeline
    out.append(_kv(p, "Planning window", f"{tl.lead_days} days before the event"))
    out.append(
        _kv(p, "Critical path", f"{tl.min_required_lead_days} days of hard dependencies", "b1")
    )
    feasible_style = "g" if tl.feasible else "r"
    out.append(
        _kv(
            p,
            "Feasible",
            "yes - the window comfortably fits the critical path"
            if tl.feasible
            else f"NO - you need at least {tl.min_required_lead_days} days",
            feasible_style,
        )
    )
    out.append(_kv(p, "Season", tl.season.value))
    out.append(_kv(p, "Critical load", f"{tl.critical_load_days} working days cannot slip at all"))
    if tl.critical_path:
        titles = []
        for task_id in tl.critical_path:
            task = tl.task(task_id)
            titles.append(task.title if task is not None else task_id)
        out.append("")
        out.append("  The chain that decides your date:")
        for i, title in enumerate(titles, 1):
            out.append("    " + p.p("r", f"{i}.") + f" {title}")
    out.append("")

    upcoming = sorted(tl.tasks, key=lambda t: t.due)[:12]
    if upcoming:
        out.append("  Next things on the clock:")
        for task in upcoming:
            mark = p.p("r", "*") if task.critical else p.p("d", "o")
            out.append(
                f"    {mark} {task.due:%d %b}  {task.title}  {p.p('d', f'({task.owner.value})')}"
            )
        if len(tl.tasks) > len(upcoming):
            out.append(p.p("d", f"    ... and {len(tl.tasks) - len(upcoming)} more"))
    if tl.run_of_show:
        out.append("")
        out.append("  Day-of run sheet:")
        for slot in tl.run_of_show[:8]:
            day = "" if slot.day_offset == 0 else f" (day {slot.day_offset:+d})"
            out.append(f"    {slot.start:%H:%M}-{slot.end:%H:%M}{day}  {slot.title}")
    out.append("")

    # -- Vendors -----------------------------------------------------------
    out.append(_rule(p, f"Vendors ({len(plan.vendors.items)} categories)"))
    for v in sorted(plan.vendors.items, key=lambda v: v.book_by):
        label = v.label[:30].ljust(30)
        out.append(
            "  "
            + p.p("b1", label)
            + " "
            + p.p("c", f"{v.deposit_pct:.0f}% deposit")
            + p.p("d", f"  book by {v.book_by:%d %b %Y}")
        )
        out.extend(_wrap(v.why, indent="        "))
        if v.negotiation_tip:
            out.append(p.p("g", "        tip: ") + v.negotiation_tip)
    if plan.vendors.milestones:
        out.append("")
        out.append(f"  Cash flow - {len(plan.vendors.milestones)} dated payments:")
        for m in plan.vendors.milestones[:8]:
            out.append(
                f"    {m.due:%d %b %Y}  {m.label[:34]:<34} "
                + p.p("c", _money(m.amount_minor, money).rjust(14))
            )
        out.append(
            "    "
            + p.p("d", "deposits total: ")
            + p.p("c", _money(plan.vendors.deposit_total_minor, money))
        )
    out.append("")

    # -- Risks -------------------------------------------------------------
    if plan.risks.items:
        out.append(_rule(p, "Top risks"))
        for r in plan.risks.top(4):
            sev_style = {"critical": "r", "high": "r", "medium": "y", "low": "d"}.get(
                r.severity.value, ""
            )
            out.append(
                "  "
                + p.p(sev_style, f"[{r.severity.value.upper():<8}]")
                + f" {r.title} "
                + p.p("d", f"(likelihood {r.likelihood}/5 x impact {r.impact}/5)")
            )
            out.extend(_wrap(r.mitigation, indent="        "))
            if r.trigger_sign:
                out.append(p.p("d", f"        watch for: {r.trigger_sign}"))
        out.append("")

    # -- Ideas -------------------------------------------------------------
    if plan.ideas:
        out.append(_rule(p, "Personalised ideas"))
        for idea in plan.ideas:
            src = "model" if idea.source.value == "model" else "catalogue"
            out.append("  " + p.p("m", f"* {idea.name}") + p.p("d", f"  [{src}]"))
            out.extend(_wrap(idea.tagline, indent="      "))
            if idea.palette:
                out.append(p.p("d", "      palette: ") + ", ".join(idea.palette))
            if idea.decor:
                out.extend(_wrap(f"Decor: {idea.decor}", indent="      "))
            if idea.menu:
                out.extend(_wrap(f"Menu: {idea.menu}", indent="      "))
            delta = idea.estimated_delta_pct
            delta_s = f"{delta:+.1f}%" if delta else "no change"
            out.append(p.p("d", "      cost: ") + f"{delta_s} - {idea.cost_fit}")
            if idea.rationale:
                out.append(p.p("d", "      why you: ") + idea.rationale)
        out.append("")

    # -- Worth knowing -----------------------------------------------------
    # Risk items already have their own section above, so skip the mirrored
    # warnings rather than making the host read the same thing twice.
    other = [w for w in plan.all_warnings if w.section != "risks"]
    if other:
        out.append(_rule(p, "Worth knowing"))
        for w in other:
            style = {"critical": "r", "high": "r", "medium": "y", "low": "d"}.get(
                w.severity.value, ""
            )
            out.append("  " + p.p(style, "!") + f" {w.message}")
            if w.suggestion:
                out.extend(_wrap(w.suggestion, indent="      "))
        out.append("")

    # -- Footer ------------------------------------------------------------
    out.append(_rule(p))
    out.append(
        p.p(
            "d",
            f"  {len(plan.timeline.tasks)} tasks · {len(plan.vendors.items)} vendor categories · "
            f"{len(plan.ideas)} ideas · ai: {plan.ai_provider}"
            f"{'' if not plan.ai_used else ' (used)'}",
        )
    )
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def demo_brief() -> Brief:
    """The story the demo tells: a Pune wedding, tight-ish budget, eco-minded couple."""
    today = date.today()
    # Aim ~9 months out, on the first Sunday of June next year.
    target = date(today.year + (1 if (today.month, today.day) > (6, 7) else 0), 6, 1)
    while target.weekday() != 6:
        target += timedelta(days=1)

    return Brief(
        event_type=EventType.WEDDING,
        event_date=target,
        guest_count=250,
        budget_total_minor=1_500_000_00,  # Rs. 15,00,000
        currency=Currency.INR,
        city_tier=CityTier.TIER_2,
        location="Pune",
        title="Aarav & Meera",
        host_name="The Raghunath family",
        setting=WindExposure.OPEN_AIR,
        priorities={
            "catering": Priority.HIGH,
            "decor": Priority.NORMAL,
            "photography": Priority.HIGH,
            "entertainment": Priority.LOW,
        },
        notes="Courtyard under old trees rather than a banquet hall. Simple, warm, close-knit.",
    )


def _provider_for(settings: Settings) -> Any:
    """Resolve a provider; return ``None`` to force the offline catalogue."""
    if not settings.ai_enabled:
        return None
    return resolve_provider(settings)


def _emit(plan: Plan, p: Painter, args: argparse.Namespace, out_dir: str | None) -> int:
    if getattr(args, "json", False):
        print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        for line in render_plan(plan, p):
            print(line)
    if out_dir:
        written = write_plan(plan, out_dir, stem=plan.brief.event_type.value)
        if not getattr(args, "json", False):
            print(p.p("d", "\n  written:"))
            for path in written:
                print(p.p("d", f"    {path}"))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    p = Painter(_use_color(args))
    if not args.json:
        print(p.p("b1", f"\n  Sutradhar {__version__} - celebration & event planner"))
        print(p.p("d", "  deterministic engines, optional LLM, complete offline fallback"))
    settings = load_settings()
    if args.offline:
        settings = settings.with_overrides(ai_enabled=False)
    plan = plan_event(demo_brief(), settings=settings, today=date.today())
    return _emit(plan, p, args, args.out)


def cmd_plan(args: argparse.Namespace) -> int:
    p = Painter(_use_color(args))
    brief = brief_from_dict(json.loads(Path(args.brief).read_text(encoding="utf-8")))
    settings = load_settings()
    if args.offline:
        settings = settings.with_overrides(ai_enabled=False)
    plan = plan_event(brief, settings=settings, today=date.today())
    return _emit(plan, p, args, args.out)


def cmd_brief(args: argparse.Namespace) -> int:
    p = Painter(_use_color(args))
    text = args.text or sys.stdin.read()
    settings = load_settings()
    if args.offline:
        settings = settings.with_overrides(ai_enabled=False)

    provider = _provider_for(settings)
    if provider is None:
        print(
            "error: parsing a natural-language brief needs a model; "
            "set SUTRADHAR_PROVIDER or drop --offline",
            file=sys.stderr,
        )
        return 2
    brief, message = try_brief_from_text(text, provider, settings)
    if brief is None:
        print(f"error: {message}", file=sys.stderr)
        return 2
    if not args.json:
        print(p.p("d", f"  {message}"))
    plan = plan_event(brief, settings=settings, today=date.today())
    return _emit(plan, p, args, args.out)


def cmd_export(args: argparse.Namespace) -> int:
    p = Painter(_use_color(args))
    brief = brief_from_dict(json.loads(Path(args.brief).read_text(encoding="utf-8")))
    settings = load_settings().with_overrides(ai_enabled=False)
    plan = plan_event(brief, settings=settings, today=date.today())
    target = Path(args.out or "out")
    target.mkdir(parents=True, exist_ok=True)
    stem = plan.brief.event_type.value
    renderers = {
        "md": (f"{stem}.md", to_markdown),
        "ics": (f"{stem}.ics", to_ics),
        "budget.csv": (f"{stem}-budget.csv", budget_csv),
        "timeline.csv": (f"{stem}-timeline.csv", timeline_csv),
        "vendors.csv": (f"{stem}-vendors.csv", vendor_csv),
    }
    name, renderer = renderers[args.format]
    path = target / name
    path.write_text(renderer(plan), encoding="utf-8")
    print(p.p("g", f"  wrote {path}"))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    return serve(host=args.host, port=args.port, web_root=args.web_root)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def _use_color(args: argparse.Namespace) -> bool:
    if getattr(args, "no_color", False) or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _add_common(sub: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = cast(
        argparse.ArgumentParser,
        sub.add_parser(name, help=help_text, description=help_text),
    )
    parser.add_argument("--out", metavar="DIR", help="also write md/json/csv/ics files here")
    parser.add_argument("--json", action="store_true", help="print the full plan as JSON on stdout")
    parser.add_argument(
        "--no-color", action="store_true", help="disable ANSI colour (or set NO_COLOR)"
    )
    parser.add_argument(
        "--offline", action="store_true", help="never call a model; use the built-in catalogue only"
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sutradhar",
        description="Sutradhar - a deterministic, catalogue-driven celebration & event planner. "
        "The planning core works offline; a language model is optional and never required.",
    )
    parser.add_argument("--version", action="version", version=f"sutradhar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = _add_common(sub, "demo", "plan a sample wedding end-to-end and print it")
    demo.set_defaults(func=cmd_demo)

    plan_p = _add_common(sub, "plan", "plan from a structured JSON brief")
    plan_p.add_argument("--brief", required=True, metavar="FILE", help="path to a JSON brief")
    plan_p.set_defaults(func=cmd_plan)

    brief_p = _add_common(sub, "brief", "plan from a natural-language brief")
    brief_p.add_argument("--text", help="the brief in plain English (or pipe it on stdin)")
    brief_p.set_defaults(func=cmd_brief)

    export_p = _add_common(sub, "export", "export one artefact from a JSON brief")
    export_p.add_argument("--brief", required=True, metavar="FILE")
    export_p.add_argument(
        "--format", default="md", choices=["md", "ics", "budget.csv", "timeline.csv", "vendors.csv"]
    )
    export_p.set_defaults(func=cmd_export)

    web = sub.add_parser("web", help="serve the accessible web UI (no build step)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument(
        "--web-root", default=str(Path(__file__).resolve().parent.parent.parent / "web")
    )
    web.add_argument("--no-color", action="store_true")
    web.set_defaults(func=cmd_web)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON - {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - safety net; real errors are typed
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
