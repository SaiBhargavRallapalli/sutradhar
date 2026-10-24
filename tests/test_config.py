"""Configuration parsing and resource-bound tests."""

from __future__ import annotations

import os

import pytest

from sutradhar.config import Settings, load_env_file, load_settings
from sutradhar.exceptions import AIConfigError


def test_env_file_parses_quotes_exports_and_preserves_existing(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "\n# comment\nexport SUTRADHAR_PROVIDER='gemini'\nSUTRADHAR_GEMINI_MODEL=\"gemini-test\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUTRADHAR_PROVIDER", "mock")
    monkeypatch.delenv("SUTRADHAR_GEMINI_MODEL", raising=False)

    applied = load_env_file(path)
    assert applied == {"SUTRADHAR_GEMINI_MODEL": "gemini-test"}
    assert os.environ["SUTRADHAR_PROVIDER"] == "mock"
    assert load_env_file(tmp_path / "missing") == {}


@pytest.mark.parametrize("line", ["NOT_AN_ASSIGNMENT", "BAD-KEY=value"])
def test_env_file_rejects_invalid_lines(tmp_path, line: str) -> None:
    path = tmp_path / ".env"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(AIConfigError):
        load_env_file(path)


def test_settings_clamp_hostile_values_and_fall_back() -> None:
    settings = load_settings(
        {
            "SUTRADHAR_PROVIDER": "unknown",
            "SUTRADHAR_AI_MAX_REPAIRS": "999",
            "SUTRADHAR_AI_MAX_PROMPT_CHARS": "1",
            "SUTRADHAR_AI_TIMEOUT": "9999",
            "SUTRADHAR_AI_CACHE_ENTRIES": "0",
            "SUTRADHAR_AI_DAILY_CAP": "not-a-number",
            "SUTRADHAR_AI_BREAKER_THRESHOLD": "0",
            "SUTRADHAR_AI_BREAKER_COOLDOWN": "-1",
            "SUTRADHAR_OPENAI_BASE_URL": "https://example.test/v1/",
        },
        load_file=False,
    )
    assert settings.provider == "auto"
    assert settings.max_repairs == 2
    assert settings.max_prompt_chars == 500
    assert settings.timeout == 60
    assert settings.ai_cache_entries == 1
    assert settings.ai_daily_cap == 500
    assert settings.ai_breaker_threshold == 1
    assert settings.ai_breaker_cooldown == 5
    assert settings.openai_base_url == "https://example.test/v1"


def test_render_enables_trusted_proxy_and_secrets_are_not_repr() -> None:
    settings = load_settings(
        {
            "RENDER": "true",
            "SUTRADHAR_OPENAI_API_KEY": "top-secret",
            "SUTRADHAR_AI_ENABLED": "false",
        },
        load_file=False,
    )
    assert settings.trust_proxy is True
    assert settings.ai_enabled is False
    assert settings.has_openai is True
    assert settings.has_any_provider is True
    assert "top-secret" not in repr(settings)


def test_settings_with_overrides_is_immutable() -> None:
    original = Settings()
    changed = original.with_overrides(ai_enabled=False)
    assert original.ai_enabled is True
    assert changed.ai_enabled is False
