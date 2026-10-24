"""The GenAI layer: providers, guardrails, schema validation and prompting.

Nothing outside this package talks to a language model. Everything that does
goes through the guardrails in :mod:`sutradhar.ai.guardrails` and the validator
in :mod:`sutradhar.ai.schema`, which is what makes the trust boundary a single,
auditable place.
"""

from __future__ import annotations

from .brief import parse_nl_brief, try_brief_from_text
from .guardrails import contains_injection, redact, sanitize_model_text, screen_input
from .ideas import IdeaResult, generate_ideas, parse_ideas
from .provider import (
    GeminiProvider,
    MockProvider,
    NullProvider,
    OpenAICompatProvider,
    Provider,
    resolve_provider,
)

__all__ = [
    "Provider",
    "OpenAICompatProvider",
    "GeminiProvider",
    "MockProvider",
    "NullProvider",
    "resolve_provider",
    "generate_ideas",
    "parse_ideas",
    "IdeaResult",
    "parse_nl_brief",
    "try_brief_from_text",
    "screen_input",
    "sanitize_model_text",
    "contains_injection",
    "redact",
]
