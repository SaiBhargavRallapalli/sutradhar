"""Schema extraction and validation for model output.

Language models return prose that *contains* JSON: fenced in backticks,
preceded by a sentence, sometimes followed by an apology. Parsing that reliably
is the difference between a demo and a product, so this module does three
things in order:

1. **Extract** the JSON value from arbitrary surrounding text.
2. **Validate** it against a small declarative field spec, coercing types
   (``"3"`` -> ``3``, ``"25%"`` -> ``25.0``) and truncating over-long strings so
   one runaway field cannot blow up a plan.
3. **Report** every problem at once, which is what makes the repair loop in
   :mod:`sutradhar.ai.ideas` effective - the model is told everything that is
   wrong in a single re-prompt instead of one error per attempt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..exceptions import AISchemaError
from .guardrails import sanitize_model_text

__all__ = ["FieldSpec", "ObjectSpec", "extract_json", "validate_object"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A single expected key in a model-produced object."""

    name: str
    types: tuple[str, ...]  # any of: "str", "int", "float", "bool", "list[str]"
    required: bool = True
    default: Any = None
    max_len: int = 400
    max_items: int = 8

    @property
    def type_label(self) -> str:
        return " or ".join(self.types)


@dataclass(frozen=True, slots=True)
class ObjectSpec:
    """The shape expected of each element of a model-produced array."""

    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


_FENCE = re.compile(r"```(?:json|javascript|js)?\s*([\s\S]*?)```", re.IGNORECASE)
_LEADING_LABEL = re.compile(r"^\s*(?:json|output|result|answer)\s*:\s*", re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of ``text``.

    Handles fenced code blocks, a leading ``json:`` label, leading prose, and
    trailing commentary after the value. Raises :class:`AISchemaError` with the
    original text attached when nothing parseable can be found.
    """
    if text is None or not str(text).strip():
        raise AISchemaError("the model returned an empty response", raw=str(text or ""))

    raw = str(text).strip()

    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    raw = _LEADING_LABEL.sub("", raw).strip()

    # Try whichever JSON value starts earliest. Preferentially trying `{` would
    # mis-parse `[{"a": 1}]` as the inner object, because the brace inside the
    # array is found before the bracket that actually opens the payload.
    candidates = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    for start in sorted(candidates):
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[start:])
            return value
        except json.JSONDecodeError:
            continue

    # Last resort: the whole payload may be bare JSON with whitespace issues.
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AISchemaError(
            f"could not find valid JSON in the model response: {exc.msg}", raw=str(text)
        ) from exc


def _coerce(value: Any, spec: FieldSpec) -> tuple[Any, str | None]:
    """Coerce ``value`` into one of ``spec.types``; return ``(value, error)``."""
    if value is None:
        return None, (None if not spec.required else f"{spec.name} is required")

    for wanted in spec.types:
        if wanted == "str":
            if isinstance(value, str):
                # Sanitise here rather than trusting every caller to remember:
                # this is the single choke point all model-produced strings pass
                # through, so scrubbing markup and escapes once covers all of them.
                out = sanitize_model_text(value, max_chars=spec.max_len)
                return out, None
            if isinstance(value, (int, float, bool)):
                return sanitize_model_text(str(value), max_chars=spec.max_len), None
        elif wanted == "float":
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value), None
            if isinstance(value, str):
                match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
                if match:
                    return float(match.group(0)), None
        elif wanted == "int":
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value, None
            if isinstance(value, float):
                return int(round(value)), None
            if isinstance(value, str):
                match = re.search(r"-?\d+", value.replace(",", ""))
                if match:
                    return int(match.group(0)), None
        elif wanted == "bool":
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str):
                if value.strip().lower() in {"true", "yes", "y"}:
                    return True, None
                if value.strip().lower() in {"false", "no", "n"}:
                    return False, None
        elif wanted == "list[str]":
            items = value if isinstance(value, list) else [value]
            cleaned: list[str] = []
            for item in items[: spec.max_items]:
                if isinstance(item, str):
                    text = item.strip()[: spec.max_len]
                elif isinstance(item, (int, float)):
                    text = str(item)
                else:
                    continue
                if text:
                    cleaned.append(text)
            return cleaned, None

    return None, f"{spec.name} should be {spec.type_label}; got {type(value).__name__}"


def validate_object(data: Any, spec: ObjectSpec) -> tuple[dict[str, Any], list[str]]:
    """Validate one object against ``spec``.

    Returns ``(cleaned, errors)``. ``errors`` lists *every* problem found rather
    than stopping at the first, so a single repair round can fix them all.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return {}, [f"expected an object, got {type(data).__name__}"]

    cleaned: dict[str, Any] = {}
    for fs in spec.fields:
        if fs.name not in data:
            if fs.required:
                errors.append(f"missing required key {fs.name!r}")
            elif fs.default is not None:
                cleaned[fs.name] = fs.default
            continue
        value, error = _coerce(data[fs.name], fs)
        if error:
            errors.append(error)
        else:
            cleaned[fs.name] = value
    return cleaned, errors
