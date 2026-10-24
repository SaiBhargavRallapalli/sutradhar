"""Natural-language brief extraction.

Lets a host write one sentence - "plan a 150 guest wedding in Hyderabad on 14
Feb 2027, budget 25 lakh, outdoor, garden vibes" - and get a structured brief
back. The model does the parsing; :func:`sutradhar.inputs.brief_from_dict` does
the validating, so a hallucinated date or a negative budget is rejected before
it can reach an engine.
"""

from __future__ import annotations

from ..config import Settings
from ..exceptions import AIProviderError, AISchemaError
from ..inputs import brief_from_dict
from ..models import Brief
from .guardrails import sanitize_model_text, screen_input
from .provider import Provider
from .schema import FieldSpec, ObjectSpec, extract_json, validate_object

__all__ = ["BRIEF_SPEC", "parse_nl_brief", "try_brief_from_text"]

BRIEF_SYSTEM = """\
You convert a one-line celebration request into structured JSON.

Reply with ONLY a JSON object. No prose, no markdown fences.

Keys:
  "event_type"      one of: wedding, birthday, community, housewarming, anniversary
  "event_date"      "YYYY-MM-DD", or null if not stated
  "guest_count"     integer, or null if not stated
  "budget"          number (major units) or a string like "25 lakh", or null
  "currency"        three-letter code, default "INR"
  "city_tier"       one of: tier_1, tier_2, tier_3, global
  "location"        string, "" if not stated
  "title"           string, "" if not stated
  "setting"         one of: indoor, sheltered, open_air
  "style_keywords"  array of short style words, [] if not stated
  "palette"         array of short colour words, [] if not stated
  "dietary"         array of dietary needs, [] if not stated
  "avoid"           array of things to avoid, [] if not stated

Rules:
  - Never invent a date, budget or guest count that was not stated; use null.
  - Infer event_type only from clear wording. Default to "birthday" if unclear.
  - Keep every string under 80 characters.
"""

BRIEF_SPEC = ObjectSpec(
    fields=(
        FieldSpec("event_type", ("str",), max_len=30),
        FieldSpec("event_date", ("str",), required=False, default=None, max_len=40),
        FieldSpec("guest_count", ("int",), required=False, default=None),
        FieldSpec("budget", ("str", "float"), required=False, default=None),
        FieldSpec("currency", ("str",), required=False, default="INR", max_len=6),
        FieldSpec("city_tier", ("str",), required=False, default="tier_1", max_len=20),
        FieldSpec("location", ("str",), required=False, default="", max_len=80),
        FieldSpec("title", ("str",), required=False, default="", max_len=80),
        FieldSpec("setting", ("str",), required=False, default="indoor", max_len=20),
        FieldSpec("style_keywords", ("list[str]",), required=False, default=[], max_items=8),
        FieldSpec("palette", ("list[str]",), required=False, default=[], max_items=6),
        FieldSpec("dietary", ("list[str]",), required=False, default=[], max_items=6),
        FieldSpec("avoid", ("list[str]",), required=False, default=[], max_items=6),
    )
)


def parse_nl_brief(text: str, provider: Provider, settings: Settings) -> dict[str, object]:
    """Extract a structured brief mapping from free text.

    Raises :class:`~sutradhar.exceptions.AIProviderError` when the model cannot
    be reached and :class:`~sutradhar.exceptions.AISchemaError` when its output
    cannot be salvaged. Callers wanting a soft failure should use
    :func:`try_brief_from_text`.
    """
    screened = screen_input(text, max_chars=settings.max_prompt_chars)
    if not screened:
        raise AISchemaError("no request text was provided")

    if not provider.is_live:
        raise AIProviderError("no live provider is available for natural-language parsing")

    raw = provider.complete(BRIEF_SYSTEM, screened, temperature=0.0, timeout=settings.timeout)
    payload = extract_json(sanitize_model_text(raw, max_chars=20_000))
    cleaned, errors = validate_object(payload, BRIEF_SPEC)
    if errors:
        raise AISchemaError("; ".join(errors[:8]), raw=raw)
    return cleaned


def try_brief_from_text(
    text: str,
    provider: Provider,
    settings: Settings,
    *,
    defaults: dict[str, object] | None = None,
) -> tuple[Brief | None, str]:
    """Best-effort NL parsing that never raises.

    Returns ``(brief, message)``. On failure ``brief`` is ``None`` and
    ``message`` explains why in language a host can act on.
    """
    try:
        fields = parse_nl_brief(text, provider, settings)
    except (AIProviderError, AISchemaError, ValueError) as exc:
        return None, f"Could not read that request: {exc}"

    merged: dict[str, object] = {
        **(defaults or {}),
        **{k: v for k, v in fields.items() if v is not None},
    }
    if not merged.get("event_date"):
        return (
            None,
            "I could not find a date in that request. Please add one, e.g. 'on 14 Feb 2027'.",
        )
    if not merged.get("guest_count"):
        merged["guest_count"] = 2
    if merged.get("budget") is None:
        return (
            None,
            "I could not find a budget in that request. Please add one, e.g. 'budget 25 lakh'.",
        )

    merged["natural_language"] = text
    try:
        return brief_from_dict(merged), "Parsed your request into a structured brief."
    except ValueError as exc:
        return None, f"That request did not produce a usable brief: {exc}"
