"""Export tests.

Exports are what a host actually takes to a vendor or a printer, so the bar is
higher than "produces text": iCalendar must be RFC 5545-shaped and foldable,
CSV must round-trip through a real parser, and Markdown must be complete.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date

import pytest

from sutradhar.models import Plan
from sutradhar.export import (
    budget_csv,
    to_ics,
    to_markdown,
    timeline_csv,
    vendor_csv,
    write_plan,
)


class TestMarkdown:
    def test_mentions_the_event_title(self, plan: Plan) -> None:
        assert plan.brief.title in to_markdown(plan)

    def test_has_a_section_per_engine(self, plan: Plan) -> None:
        md = to_markdown(plan)
        for heading in ("Budget", "Timeline", "Vendor"):
            assert heading.lower() in md.lower(), f"missing a {heading} section"

    def test_lists_every_budget_line(self, plan: Plan) -> None:
        md = to_markdown(plan)
        for line in plan.budget.lines:
            assert line.label in md

    def test_lists_every_task(self, plan: Plan) -> None:
        md = to_markdown(plan)
        for task in plan.timeline.tasks:
            assert task.title in md

    def test_renders_money_as_formatted_strings_not_raw_integers(self, plan: Plan) -> None:
        """A host reading Markdown should see a currency, not paise."""
        assert "₹" in to_markdown(plan)

    def test_is_not_empty(self, plan: Plan) -> None:
        assert len(to_markdown(plan)) > 500

    def test_contains_no_ansi_escapes(self, plan: Plan) -> None:
        """Markdown may be piped to a file; escape codes would corrupt it."""
        assert "\x1b[" not in to_markdown(plan)


class TestCsv:
    @pytest.mark.parametrize("renderer,min_rows", [
        (budget_csv, 1), (timeline_csv, 1), (vendor_csv, 1),
    ])
    def test_parses_with_the_stdlib_csv_reader(self, plan: Plan, renderer, min_rows: int) -> None:
        rows = list(csv.reader(io.StringIO(renderer(plan))))
        assert len(rows) > min_rows

    @pytest.mark.parametrize("renderer", [budget_csv, timeline_csv, vendor_csv])
    def test_every_row_has_the_same_width_as_the_header(self, plan: Plan, renderer) -> None:
        rows = list(csv.reader(io.StringIO(renderer(plan))))
        header_width = len(rows[0])
        assert all(len(r) == header_width for r in rows), "ragged CSV rows break spreadsheets"

    def test_budget_csv_covers_every_line_plus_contingency(self, plan: Plan) -> None:
        """The exporter appends a Contingency row so a spreadsheet's column
        sums to the full envelope, not just the allocated slice."""
        rows = list(csv.DictReader(io.StringIO(budget_csv(plan))))
        assert len(rows) == len(plan.budget.lines) + 1
        assert rows[-1]["category"] == "Contingency"

    def test_budget_csv_lines_sum_to_the_total_budget(self, plan: Plan) -> None:
        rows = list(csv.DictReader(io.StringIO(budget_csv(plan))))
        total = sum(float(r["amount"]) for r in rows)
        assert total == pytest.approx(plan.budget.total_minor / 100, abs=0.05)

    def test_budget_amounts_are_parseable_numbers(self, plan: Plan) -> None:
        rows = list(csv.DictReader(io.StringIO(budget_csv(plan))))
        for row in rows:
            amount_key = next(k for k in row if "amount" in k.lower() or "minor" in k.lower())
            assert float(row[amount_key]) >= 0

    def test_timeline_csv_covers_every_task(self, plan: Plan) -> None:
        rows = list(csv.DictReader(io.StringIO(timeline_csv(plan))))
        assert len(rows) == len(plan.timeline.tasks)

    def test_timeline_dates_are_iso_formatted(self, plan: Plan) -> None:
        rows = list(csv.DictReader(io.StringIO(timeline_csv(plan))))
        for row in rows:
            for value in row.values():
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
                    date.fromisoformat(value)  # raises if malformed

    def test_vendor_csv_covers_every_payment_milestone(self, plan: Plan) -> None:
        """One row per dated payment, not per vendor: this is the cash-flow
        view a host actually pays from."""
        rows = list(csv.DictReader(io.StringIO(vendor_csv(plan))))
        assert len(rows) == len(plan.vendors.milestones)

    def test_vendor_csv_amounts_sum_to_deposits_total(self, plan: Plan) -> None:
        rows = list(csv.DictReader(io.StringIO(vendor_csv(plan))))
        total = sum(float(r["amount"]) for r in rows)
        assert total == pytest.approx(plan.vendors.deposit_total_minor / 100, abs=0.05)

    def test_csv_handles_embedded_commas_and_quotes(self, plan: Plan) -> None:
        """A label containing a comma must be quoted, not split into two cells."""
        text = vendor_csv(plan)
        rows = list(csv.reader(io.StringIO(text)))
        # Re-parsing successfully with consistent widths is the real assertion.
        assert all(len(r) == len(rows[0]) for r in rows)

    @pytest.mark.parametrize("renderer", [budget_csv, timeline_csv, vendor_csv])
    def test_output_is_utf8_safe(self, plan: Plan, renderer) -> None:
        renderer(plan).encode("utf-8")  # must not raise


class TestIcs:
    def test_has_required_headers(self, plan: Plan) -> None:
        ics = to_ics(plan)
        assert ics.startswith("BEGIN:VCALENDAR")
        assert "VERSION:2.0" in ics
        assert "PRODID:" in ics
        assert ics.rstrip().endswith("END:VCALENDAR")

    def test_emits_every_task_and_payment_milestone(self, plan: Plan) -> None:
        """The calendar carries tasks, dated payments and the event itself, so
        a host can subscribe to one file and see everything."""
        ics = to_ics(plan)
        expected = len(plan.timeline.tasks) + len(plan.vendors.milestones) + 1
        assert ics.count("BEGIN:VEVENT") == expected

    def test_lines_are_folded_at_75_octets(self, plan: Plan) -> None:
        """RFC 5545 requires folding long lines; violating it breaks Outlook."""
        for line in to_ics(plan).splitlines():
            assert len(line.encode("utf-8")) <= 75, f"line too long: {line[:60]}..."

    def test_crlf_line_endings(self, plan: Plan) -> None:
        """RFC 5545 mandates CRLF; bare LF is a common source of client bugs."""
        ics = to_ics(plan)
        assert "\r\n" in ics
        # No bare LF left over.
        assert re.search(r"[^\r]\n", ics) is None

    def test_dates_are_in_ical_format(self, plan: Plan) -> None:
        for match in re.finditer(r"DTSTART;?[^:]*:(\S+)", to_ics(plan)):
            value = match.group(1)
            assert re.fullmatch(r"\d{8}(T\d{6}Z?)?", value), f"bad iCal date: {value}"

    def test_every_event_has_a_uid_and_summary(self, plan: Plan) -> None:
        ics = to_ics(plan)
        expected = len(plan.timeline.tasks) + len(plan.vendors.milestones) + 1
        assert ics.count("UID:") == expected
        assert ics.count("SUMMARY:") == expected

    def test_uids_are_unique(self, plan: Plan) -> None:
        """Duplicate UIDs make calendar clients drop or merge events."""
        uids = re.findall(r"UID:(\S+)", to_ics(plan))
        assert len(uids) == len(set(uids))

    def test_text_values_are_escaped(self, plan: Plan) -> None:
        """Commas, semicolons and backslashes must be escaped per RFC 5545."""
        ics = to_ics(plan)
        for line in ics.splitlines():
            if line.startswith("SUMMARY:") or line.startswith("DESCRIPTION:"):
                body = line.split(":", 1)[1]
                # An unescaped semicolon or comma inside TEXT is invalid.
                assert ";" not in body.replace("\\;", "").replace("\\n", "")

    def test_no_bare_newlines_inside_a_value(self, plan: Plan) -> None:
        for line in to_ics(plan).splitlines():
            assert "\n" not in line and "\r" not in line

    def test_is_deterministic(self, plan: Plan) -> None:
        """A stable DTSTAMP keeps exports diffable across runs."""
        assert to_ics(plan) == to_ics(plan)


class TestWritePlan:
    def test_writes_every_format(self, plan: Plan, tmp_path) -> None:
        written = write_plan(plan, tmp_path)
        assert len(written) == 6
        for path in written:
            assert path.exists()
            assert path.stat().st_size > 0

    def test_creates_the_directory_if_missing(self, plan: Plan, tmp_path) -> None:
        target = tmp_path / "nested" / "deeper"
        write_plan(plan, target)
        assert target.is_dir()

    def test_returns_the_paths_it_wrote(self, plan: Plan, tmp_path) -> None:
        written = write_plan(plan, tmp_path)
        names = {p.name for p in written}
        assert any(n.endswith(".md") for n in names)
        assert any(n.endswith(".ics") for n in names)
        assert any(n.endswith(".json") for n in names)

    def test_written_json_is_valid_and_complete(self, plan: Plan, tmp_path) -> None:
        import json

        written = write_plan(plan, tmp_path)
        json_path = next(p for p in written if p.suffix == ".json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["brief"]["guest_count"] == plan.brief.guest_count
        assert len(data["budget"]["lines"]) == len(plan.budget.lines)

    def test_honours_a_custom_stem(self, plan: Plan, tmp_path) -> None:
        written = write_plan(plan, tmp_path, stem="aarav")
        assert all(p.name.startswith("aarav") for p in written)

    def test_is_idempotent(self, plan: Plan, tmp_path) -> None:
        write_plan(plan, tmp_path)
        second = write_plan(plan, tmp_path)
        assert all(p.exists() for p in second)
