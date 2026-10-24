"""Typed exception hierarchy for Sutradhar.

Every failure mode a caller can reasonably hit has its own exception class so
that the CLI and web layers can map errors to useful, human-readable messages
and correct exit codes without ever string-matching on error text.
"""

from __future__ import annotations

__all__ = [
    "AIConfigError",
    "AIGuardrailError",
    "AIProviderError",
    "AISchemaError",
    "BriefError",
    "BudgetError",
    "CatalogError",
    "SutradharError",
    "TimelineError",
    "ValidationError",
]


class SutradharError(Exception):
    """Base class for every error raised by Sutradhar."""


class ValidationError(SutradharError):
    """Raised when user-supplied input fails validation.

    ``field`` and ``message`` are kept separate so interfaces can render a
    labelled, screen-reader friendly error instead of a raw traceback.
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class BriefError(ValidationError):
    """Raised when an event brief is structurally incomplete or contradictory."""


class TimelineError(SutradharError):
    """Raised when a task dependency graph is invalid (cycle, unknown id)."""


class BudgetError(SutradharError):
    """Raised when a budget cannot be allocated under the given constraints."""


class CatalogError(SutradharError):
    """Raised when catalog data is missing or inconsistent for an event type."""


class AIProviderError(SutradharError):
    """Raised when a model call fails after retries (network, auth, 5xx)."""


class AIConfigError(AIProviderError):
    """Raised when the requested provider is not configured on this machine."""


class AIGuardrailError(SutradharError):
    """Raised when input or model output trips a safety guardrail."""


class AISchemaError(SutradharError):
    """Raised when model output cannot be coerced into the required schema."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        self.raw = raw
        super().__init__(message)
