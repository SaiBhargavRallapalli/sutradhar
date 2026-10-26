"""Runtime configuration, read from the process environment.

Design rules that keep this safe:

* **Secrets are never persisted.** Keys come from the environment (optionally
  seeded by a git-ignored ``.env``), are marked ``repr=False`` so they can never
  leak into a log line or a traceback, and are scrubbed again by
  :func:`sutradhar.ai.guardrails.redact` before any text is printed.
* **Sane, clamped defaults.** Malformed env values degrade to defaults instead
  of crashing, and numeric limits are clamped so a hostile ``.env`` cannot make
  the process loop forever or allocate unbounded memory.
* **Offline by default.** With no key configured, planning still works end to
  end using the deterministic engines and the curated idea catalogue.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .exceptions import AIConfigError

__all__ = ["DEFAULT_ENV_FILE", "Settings", "load_env_file", "load_settings"]


DEFAULT_ENV_FILE = ".env"

_TRUE = {"1", "true", "yes", "on", "y"}

#: Shortest value that can be a matched pair of surrounding quote characters.
_MIN_QUOTED_LEN = 2


def load_env_file(path: str | Path = DEFAULT_ENV_FILE, *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a dotenv-style file into ``os.environ``.

    A deliberately small parser: comments and blank lines are skipped, optional
    surrounding quotes are stripped, and values never expand shell variables.
    Existing environment variables win unless ``override`` is true - this is what
    makes ``docker run -e SUTRADHAR_PROVIDER=mock`` beat a checked-in ``.env``.

    Returns the mapping of keys that were newly set (useful in tests).
    """
    file_path = Path(path)
    applied: dict[str, str] = {}
    if not file_path.is_file():
        return applied

    for lineno, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            raise AIConfigError(f"{file_path}:{lineno}: expected KEY=VALUE, got {stripped!r}")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= _MIN_QUOTED_LEN and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key.isidentifier():
            raise AIConfigError(
                f"{file_path}:{lineno}: {key!r} is not a valid environment variable name"
            )
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(raw: str | None, default: int, low: int, high: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(low, min(high, int(raw.strip())))
    except ValueError:
        return default


def _float(raw: str | None, default: float, low: float, high: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(low, min(high, float(raw.strip())))
    except ValueError:
        return default


def _optional(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of runtime configuration."""

    provider: str = "auto"
    ai_enabled: bool = True
    max_repairs: int = 1
    max_prompt_chars: int = 6000
    timeout: float = 40.0
    ai_cache_entries: int = 128
    ai_cache_ttl: float = 21_600.0
    ai_daily_cap: int = 500
    ai_breaker_threshold: int = 3
    ai_breaker_cooldown: float = 60.0
    trust_proxy: bool = False

    # OpenAI-compatible chat-completions endpoint.
    openai_api_key: str | None = field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # Google Gemini generateContent endpoint.
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-2.5-flash"

    @property
    def has_openai(self) -> bool:
        """True when an OpenAI-compatible API key is configured."""
        return self.openai_api_key is not None

    @property
    def has_gemini(self) -> bool:
        """True when a Gemini API key is configured."""
        return self.gemini_api_key is not None

    @property
    def has_any_provider(self) -> bool:
        """True when at least one live provider is configured."""
        return self.has_openai or self.has_gemini

    def with_overrides(self, **kwargs: object) -> Settings:
        """Return a copy with ``kwargs`` applied (used by the CLI ``--no-ai`` flag)."""
        # ``dataclasses.replace`` is typed per-field, so mypy cannot narrow a
        # ``**kwargs: object`` bag down to those types. The ignore is scoped to
        # that one diagnostic rather than silencing the line wholesale.
        return replace(self, **kwargs)  # type: ignore[arg-type]


def load_settings(
    environ: dict[str, str] | None = None,
    *,
    env_file: str | Path | None = DEFAULT_ENV_FILE,
    load_file: bool = True,
) -> Settings:
    """Build :class:`Settings` from ``environ`` (default: ``os.environ``).

    Parameters
    ----------
    environ:
        Mapping to read from. Tests pass an explicit dict to stay hermetic.
    env_file:
        Dotenv file to seed ``os.environ`` from before reading. ``None`` skips it.
    load_file:
        Set ``False`` to ignore dotenv files entirely (e.g. in CI or when the
        host application already manages its own environment).
    """
    if load_file and env_file is not None:
        load_env_file(env_file)
    env = os.environ if environ is None else environ

    provider = (env.get("SUTRADHAR_PROVIDER") or "auto").strip().lower()
    if provider not in {"auto", "openai", "gemini", "mock"}:
        # Unknown providers fall back to `auto` rather than failing the run.
        provider = "auto"

    return Settings(
        provider=provider,
        ai_enabled=_bool(env.get("SUTRADHAR_AI_ENABLED"), True),
        max_repairs=_int(env.get("SUTRADHAR_AI_MAX_REPAIRS"), 1, 0, 2),
        max_prompt_chars=_int(env.get("SUTRADHAR_AI_MAX_PROMPT_CHARS"), 6000, 500, 100_000),
        timeout=_float(env.get("SUTRADHAR_AI_TIMEOUT"), 40.0, 5.0, 60.0),
        ai_cache_entries=_int(env.get("SUTRADHAR_AI_CACHE_ENTRIES"), 128, 1, 1_024),
        ai_cache_ttl=_float(env.get("SUTRADHAR_AI_CACHE_TTL"), 21_600.0, 60.0, 86_400.0),
        ai_daily_cap=_int(env.get("SUTRADHAR_AI_DAILY_CAP"), 500, 1, 100_000),
        ai_breaker_threshold=_int(env.get("SUTRADHAR_AI_BREAKER_THRESHOLD"), 3, 1, 20),
        ai_breaker_cooldown=_float(env.get("SUTRADHAR_AI_BREAKER_COOLDOWN"), 60.0, 5.0, 3_600.0),
        trust_proxy=_bool(env.get("SUTRADHAR_TRUST_PROXY"), bool(env.get("RENDER"))),
        openai_api_key=_optional(env.get("SUTRADHAR_OPENAI_API_KEY")),
        openai_base_url=(
            env.get("SUTRADHAR_OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/"),
        openai_model=(env.get("SUTRADHAR_OPENAI_MODEL") or "gpt-4o-mini").strip(),
        gemini_api_key=_optional(env.get("SUTRADHAR_GEMINI_API_KEY")),
        gemini_model=(env.get("SUTRADHAR_GEMINI_MODEL") or "gemini-2.5-flash").strip(),
    )
