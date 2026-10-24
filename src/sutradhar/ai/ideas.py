"""Model-backed concept generation, with validation and grounding.

The flow is the safety-critical part of the product, so it is explicit:

``screen -> prompt -> call -> extract -> validate -> (repair)* -> ground -> return``

At every failure point the pipeline degrades gracefully to the curated
catalogue rather than failing the plan. A host with no API key, a flaky
network, or a model that returns a haiku instead of JSON still gets a complete,
budget-grounded set of ideas - they just come from the catalogue instead.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from ..catalog.specs import EventTypeProfile
from ..config import Settings
from ..engines.personalize import catalogue_ideas, ground_concepts, idea_warnings
from ..enums import RiskLevel
from ..exceptions import AIGuardrailError, AIProviderError, AISchemaError
from ..models import Brief, BudgetPlan, IdeaConcept, PlanWarning
from .guardrails import sanitize_model_text, screen_input
from .prompts import IDEAS_SYSTEM, build_ideas_user, build_repair_user
from .provider import Provider
from .schema import FieldSpec, ObjectSpec, extract_json, validate_object

__all__ = ["IDEAS_SPEC", "IdeaResult", "generate_ideas", "parse_ideas"]


#: The exact shape the system prompt promises. Kept beside the prompt so the
#: two can drift only deliberately, never accidentally.
IDEAS_SPEC = ObjectSpec(
    fields=(
        FieldSpec("name", ("str",), max_len=60),
        FieldSpec("tagline", ("str",), max_len=140),
        FieldSpec("palette", ("list[str]",), required=False, default=[], max_items=5),
        FieldSpec("decor", ("str",), required=False, default="", max_len=220),
        FieldSpec("menu", ("str",), required=False, default="", max_len=220),
        FieldSpec("favors", ("str",), required=False, default="", max_len=160),
        FieldSpec("attire", ("str",), required=False, default="", max_len=160),
        FieldSpec("music", ("str",), required=False, default="", max_len=160),
        FieldSpec("activities", ("str",), required=False, default="", max_len=220),
        FieldSpec("estimated_delta_pct", ("float",), required=False, default=0.0),
    )
)

#: Never let the model return an unbounded list, whatever it decides to do.
_MAX_CONCEPTS = 3

#: Ceiling on a single model response before we attempt to parse it.
_MAX_RESPONSE_CHARS = 40_000

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class IdeaResult:
    """Concepts plus the diagnostics needed to explain their provenance."""

    ideas: tuple[IdeaConcept, ...]
    warnings: tuple[PlanWarning, ...] = ()
    used_model: bool = False
    provider_name: str = "catalogue"


def _slugify(text: str) -> str:
    slug = _SLUG.sub("-", text.lower()).strip("-")
    return slug[:48] or "concept"


def parse_ideas(text: str) -> list[IdeaConcept]:
    """Extract and validate concepts from raw model text.

    Raises :class:`~sutradhar.exceptions.AISchemaError` listing *every*
    problem, so a single repair round can address all of them at once.
    """
    payload = extract_json(text)
    if not isinstance(payload, list):
        raise AISchemaError(
            f"expected a JSON array of concepts, got {type(payload).__name__}", raw=text
        )
    if not payload:
        raise AISchemaError("the model returned an empty concept list", raw=text)

    concepts: list[IdeaConcept] = []
    errors: list[str] = []
    for index, item in enumerate(payload[:_MAX_CONCEPTS], start=1):
        cleaned, item_errors = validate_object(item, IDEAS_SPEC)
        if item_errors:
            errors.extend(f"concept {index}: {error}" for error in item_errors)
            continue
        concepts.append(
            IdeaConcept(
                key=_slugify(str(cleaned["name"])),
                # name and tagline are the two most visible fields in every
                # interface, so they get the same scrubbing as the rest.
                name=sanitize_model_text(str(cleaned["name"]), max_chars=80),
                tagline=sanitize_model_text(str(cleaned["tagline"]), max_chars=200),
                palette=tuple(cleaned.get("palette") or ()),
                decor=sanitize_model_text(cleaned.get("decor", "")),
                menu=sanitize_model_text(cleaned.get("menu", "")),
                favors=sanitize_model_text(cleaned.get("favors", "")),
                attire=sanitize_model_text(cleaned.get("attire", "")),
                music=sanitize_model_text(cleaned.get("music", "")),
                activities=sanitize_model_text(cleaned.get("activities", "")),
                estimated_delta_pct=float(cleaned.get("estimated_delta_pct") or 0.0),
            )
        )

    if errors:
        raise AISchemaError("; ".join(errors[:12]), raw=text)
    return concepts


def _screened_context(brief: Brief, settings: Settings) -> tuple[str, list[PlanWarning]]:
    """Screen every free-text field the host can influence before prompting."""
    chunks = [brief.natural_language, brief.notes, brief.taste.vibe, *brief.taste.avoid]
    joined = " ".join(chunk for chunk in chunks if chunk)

    try:
        return screen_input(joined, max_chars=settings.max_prompt_chars), []
    except AIGuardrailError as exc:
        return "", [
            PlanWarning(
                code="guardrail_input",
                message=f"Free-text preferences were not sent to the model: {exc}",
                severity=RiskLevel.MEDIUM,
                section="ideas",
                suggestion="Rephrase the request without instruction-like wording to get tailored ideas.",
            )
        ]


def generate_ideas(
    brief: Brief,
    budget: BudgetPlan,
    profile: EventTypeProfile,
    provider: Provider,
    settings: Settings,
    *,
    limit: int = 3,
) -> IdeaResult:
    """Produce budget-grounded concepts, preferring the model when available."""
    if not settings.ai_enabled or not provider.is_live:
        return IdeaResult(
            ideas=catalogue_ideas(brief, budget, profile, limit=limit),
            used_model=False,
            provider_name="catalogue",
        )

    screened, warnings = _screened_context(brief, settings)
    if warnings:
        # A tripped guardrail means the free text is not trusted; fall back so
        # the host still receives ideas rather than nothing at all.
        return IdeaResult(
            ideas=catalogue_ideas(brief, budget, profile, limit=limit),
            warnings=tuple(warnings),
            used_model=False,
            provider_name="catalogue",
        )

    # Ranked once and reused: the names steer the model away from repeats,
    # and the concepts themselves are the fallback if the model path fails.
    catalogue = catalogue_ideas(brief, budget, profile, limit=6)

    user_prompt = build_ideas_user(
        brief,
        budget,
        profile,
        screened,
        existing_names=tuple(idea.name for idea in catalogue),
    )

    raw = ""
    errors: list[str] = []
    deadline = time.monotonic() + settings.timeout
    for attempt in range(settings.max_repairs + 1):
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            warnings.append(
                PlanWarning(
                    code="model_timeout",
                    message="The model exhausted its total time budget; using catalogue ideas.",
                    severity=RiskLevel.MEDIUM,
                    section="ideas",
                    suggestion="Try again later or continue with the complete catalogue plan.",
                )
            )
            break
        prompt = user_prompt if attempt == 0 else build_repair_user(user_prompt, raw, errors)

        try:
            raw = provider.complete(IDEAS_SYSTEM, prompt, temperature=0.7, timeout=remaining)
        except AIProviderError as exc:
            warnings.append(
                PlanWarning(
                    code="model_unavailable",
                    message=f"Language model unavailable ({exc}). Using curated catalogue ideas instead.",
                    severity=RiskLevel.MEDIUM,
                    section="ideas",
                    suggestion="Check the provider configuration, or continue with the catalogue ideas.",
                )
            )
            break

        try:
            concepts = parse_ideas(sanitize_model_text(raw, max_chars=_MAX_RESPONSE_CHARS))
        except AISchemaError as exc:
            errors = str(exc).split("; ")
            if attempt >= settings.max_repairs:
                warnings.append(
                    PlanWarning(
                        code="model_schema",
                        message=(
                            f"The model returned unusable output after {settings.max_repairs} "
                            "correction attempts; using curated catalogue ideas."
                        ),
                        severity=RiskLevel.LOW,
                        section="ideas",
                        suggestion="Re-run, or switch to a model with stronger JSON output.",
                    )
                )
                break
            continue

        grounded = ground_concepts(list(concepts), budget, brief.taste)[:limit]
        warnings.extend(idea_warnings(grounded))
        return IdeaResult(
            ideas=grounded,
            warnings=tuple(warnings),
            used_model=True,
            provider_name=provider.name,
        )

    return IdeaResult(
        ideas=catalogue[:limit],
        warnings=tuple(warnings),
        used_model=False,
        provider_name="catalogue",
    )
