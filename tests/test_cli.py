"""Command-level tests for every CLI verb and error boundary."""

from __future__ import annotations

import json
from pathlib import Path

from sutradhar import cli
from sutradhar.config import Settings
from sutradhar.inputs import brief_from_dict


def _brief_payload() -> dict[str, object]:
    return {
        "event_type": "birthday",
        "event_date": "2027-04-10",
        "guest_count": 40,
        "budget": "200000",
        "currency": "INR",
        "location": "Bengaluru",
        "city_tier": "tier_1",
        "setting": "indoor",
    }


def _brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(_brief_payload()), encoding="utf-8")
    return path


def test_demo_renders_full_offline_plan(capsys) -> None:
    assert cli.main(["demo", "--offline", "--no-color"]) == 0
    output = capsys.readouterr().out
    assert "Budget" in output
    assert "Critical path" in output
    assert "unallocated" in output


def test_demo_json_is_machine_readable(capsys) -> None:
    assert cli.main(["demo", "--offline", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["budget"]["unallocated_minor"] == 0


def test_plan_reads_json_and_writes_all_artifacts(tmp_path, capsys) -> None:
    brief_path = _brief_file(tmp_path)
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "plan",
                "--brief",
                str(brief_path),
                "--offline",
                "--no-color",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "written:" in capsys.readouterr().out
    assert {path.suffix for path in out.iterdir()} >= {".md", ".json", ".csv", ".ics"}


def test_export_writes_requested_format(tmp_path, capsys) -> None:
    brief_path = _brief_file(tmp_path)
    out = tmp_path / "export"
    assert (
        cli.main(
            [
                "export",
                "--brief",
                str(brief_path),
                "--format",
                "budget.csv",
                "--out",
                str(out),
                "--no-color",
            ]
        )
        == 0
    )
    exported = out / "birthday-budget.csv"
    assert exported.is_file()
    assert "wrote" in capsys.readouterr().out


def test_plain_text_brief_explains_offline_requirement(capsys) -> None:
    assert cli.main(["brief", "--offline", "--text", "a small birthday"]) == 2
    assert "needs a model" in capsys.readouterr().err


def test_plain_text_brief_success_path(monkeypatch, capsys) -> None:
    brief = brief_from_dict(_brief_payload())
    monkeypatch.setattr(cli, "load_settings", lambda: Settings(provider="mock"))
    monkeypatch.setattr(
        cli,
        "try_brief_from_text",
        lambda *_args: (brief, "Parsed the event brief."),
    )
    assert cli.main(["brief", "--text", "a birthday", "--no-color"]) == 0
    output = capsys.readouterr().out
    assert "Parsed the event brief." in output
    assert "BIRTHDAY" in output


def test_web_delegates_to_production_server(monkeypatch) -> None:
    called: dict[str, object] = {}

    def fake_serve(*, host: str, port: int, web_root: str | None) -> int:
        called.update(host=host, port=port, web_root=web_root)
        return 0

    monkeypatch.setattr(cli, "serve", fake_serve)
    assert cli.main(["web", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 9999
    assert called["web_root"] is None


def test_missing_and_invalid_brief_files_are_actionable(tmp_path, capsys) -> None:
    assert cli.main(["plan", "--brief", str(tmp_path / "missing.json"), "--offline"]) == 2
    assert "error:" in capsys.readouterr().err

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    assert cli.main(["plan", "--brief", str(invalid), "--offline"]) == 2
    assert "invalid JSON" in capsys.readouterr().err
