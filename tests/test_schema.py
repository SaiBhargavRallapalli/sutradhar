"""Schema extraction and validation tests.

The model layer's contract is: whatever a model returns, we either repair it
into the expected shape or reject it with an error a human can act on. Never
silently accept junk, and never crash on odd-but-fixable output.
"""
from __future__ import annotations

import json

import pytest

from sutradhar.ai.schema import FieldSpec, ObjectSpec, extract_json, validate_object
from sutradhar.exceptions import AISchemaError

IDEA_SPEC = ObjectSpec(
    fields=(
        FieldSpec("name", ("str",), required=True, max_len=80),
        FieldSpec("tagline", ("str",), required=True, max_len=200),
        FieldSpec("palette", ("list[str]",), required=False, max_items=6),
        FieldSpec("delta_pct", ("float",), required=False, default=0.0),
        FieldSpec("highlight", ("bool",), required=False, default=False),
    )
)


class TestExtractJson:
    def test_extracts_a_bare_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_extracts_from_a_fenced_block(self) -> None:
        text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps!'
        assert extract_json(text) == {"a": 1}

    def test_extracts_from_an_unlabelled_fence(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_tolerates_a_leading_label(self) -> None:
        assert extract_json('json: {"a": 1}') == {"a": 1}

    def test_tolerates_leading_prose(self) -> None:
        assert extract_json('Sure! Here is the plan:\n{"a": 1}') == {"a": 1}

    def test_tolerates_trailing_commentary(self) -> None:
        assert extract_json('{"a": 1} - let me know if you want changes!') == {"a": 1}

    def test_extracts_an_array(self) -> None:
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_finds_an_array_nested_in_prose(self) -> None:
        assert extract_json('ideas below\n[{"a": 1}]') == [{"a": 1}]

    def test_handles_unicode(self) -> None:
        assert extract_json('{"name": "सूत्रधार"}') == {"name": "सूत्रधार"}

    def test_handles_nested_structures(self) -> None:
        inner = {"a": {"b": [1, 2, {"c": 3}]}}
        assert extract_json(json.dumps(inner)) == inner

    def test_empty_response_raises(self) -> None:
        with pytest.raises(AISchemaError):
            extract_json("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(AISchemaError):
            extract_json("   \n  ")

    def test_no_json_raises_with_the_raw_text_attached(self) -> None:
        with pytest.raises(AISchemaError) as exc:
            extract_json("I am afraid I cannot help with that.")
        # The raw text is attached so a caller can log what went wrong.
        assert exc.value.raw

    def test_truncated_json_raises(self) -> None:
        with pytest.raises(AISchemaError):
            extract_json('{"name": "Marigold", "tagl')

    def test_prefers_the_first_value_when_several_appear(self) -> None:
        assert extract_json('{"a": 1} and also {"a": 2}') == {"a": 1}


class TestValidateObject:
    def test_accepts_a_well_formed_object(self) -> None:
        out, errors = validate_object(
            {"name": "Marigold", "tagline": "Warm and earthy", "palette": ["marigold", "clay"]},
            IDEA_SPEC,
        )
        assert errors == []
        assert out["name"] == "Marigold"
        assert out["palette"] == ["marigold", "clay"]

    def test_reports_a_missing_required_field(self) -> None:
        _, errors = validate_object({"name": "Marigold"}, IDEA_SPEC)
        assert any("tagline" in e for e in errors)

    def test_fills_defaults_for_absent_optional_fields(self) -> None:
        out, errors = validate_object({"name": "a", "tagline": "b"}, IDEA_SPEC)
        assert errors == []
        assert out["delta_pct"] == 0.0
        assert out["highlight"] is False

    def test_coerces_a_numeric_string_to_float(self) -> None:
        out, errors = validate_object(
            {"name": "a", "tagline": "b", "delta_pct": "+4.5%"}, IDEA_SPEC
        )
        assert errors == []
        assert out["delta_pct"] == 4.5

    def test_coerces_an_int_to_float(self) -> None:
        out, _ = validate_object({"name": "a", "tagline": "b", "delta_pct": 3}, IDEA_SPEC)
        assert out["delta_pct"] == 3.0

    def test_coerces_a_float_to_int_when_int_is_expected(self) -> None:
        spec = ObjectSpec((FieldSpec("count", ("int",)),))
        out, errors = validate_object({"count": 3.7}, spec)
        assert errors == []
        assert out["count"] == 4

    def test_coerces_scalars_to_string(self) -> None:
        out, _ = validate_object({"name": 42, "tagline": "b"}, IDEA_SPEC)
        assert out["name"] == "42"

    def test_rejects_a_bool_where_a_number_is_wanted(self) -> None:
        """`True` must not quietly become `1.0` - that is a type confusion."""
        out, errors = validate_object(
            {"name": "a", "tagline": "b", "delta_pct": True}, IDEA_SPEC
        )
        assert errors or out["delta_pct"] is None or out["delta_pct"] == 0.0

    def test_coerces_boolean_strings(self) -> None:
        spec = ObjectSpec((FieldSpec("flag", ("bool",)),))
        out, _ = validate_object({"flag": "yes"}, spec)
        assert out["flag"] is True
        out, _ = validate_object({"flag": "No"}, spec)
        assert out["flag"] is False

    def test_wraps_a_scalar_into_a_list(self) -> None:
        out, errors = validate_object(
            {"name": "a", "tagline": "b", "palette": "marigold"}, IDEA_SPEC
        )
        assert errors == []
        assert out["palette"] == ["marigold"]

    def test_truncates_an_overlong_list(self) -> None:
        out, _ = validate_object(
            {"name": "a", "tagline": "b", "palette": [str(i) for i in range(20)]}, IDEA_SPEC
        )
        assert len(out["palette"]) == 6

    def test_truncates_an_overlong_string(self) -> None:
        out, _ = validate_object({"name": "x" * 500, "tagline": "b"}, IDEA_SPEC)
        assert len(out["name"]) == 80

    def test_strips_html_from_a_string_field(self) -> None:
        out, _ = validate_object({"name": "<b>Marigold</b>", "tagline": "b"}, IDEA_SPEC)
        assert "<b>" not in out["name"]
        assert "Marigold" in out["name"]

    def test_drops_unknown_keys(self) -> None:
        out, _ = validate_object(
            {"name": "a", "tagline": "b", "dangerous": "payload"}, IDEA_SPEC
        )
        assert "dangerous" not in out

    def test_non_object_input_is_reported_not_crashed(self) -> None:
        _, errors = validate_object("not an object", IDEA_SPEC)
        assert errors

    def test_none_input_is_reported(self) -> None:
        _, errors = validate_object(None, IDEA_SPEC)
        assert errors

    def test_empty_spec_passes_any_object_through(self) -> None:
        out, errors = validate_object({"anything": 1}, ObjectSpec())
        assert errors == []

    def test_collects_multiple_errors_at_once(self) -> None:
        """Reporting every problem beats reporting the first and making the
        caller iterate."""
        _, errors = validate_object({}, IDEA_SPEC)
        assert len(errors) >= 2

    def test_errors_are_readable_sentences(self) -> None:
        _, errors = validate_object({}, IDEA_SPEC)
        for e in errors:
            assert isinstance(e, str) and e.strip()
            assert not e.startswith("("), "errors should not leak tuple reprs"


class TestRepairRoundTrip:
    def test_a_messy_model_response_survives_the_pipeline(self) -> None:
        """The realistic worst case: prose, a fence, wrong types, extra keys."""
        raw = '''Here are your ideas:

```json
[
  {
    "name": "Marigold & Terracotta",
    "tagline": "Sun-warmed and earthy",
    "palette": "marigold, terracotta, brass",
    "delta_pct": "+2.5%",
    "highlight": "yes",
    "unwanted": "<script>alert(1)</script>"
  }
]
```'''
        ideas = extract_json(raw)
        assert isinstance(ideas, list)
        out, errors = validate_object(ideas[0], IDEA_SPEC)
        assert errors == []
        assert out["palette"] == ["marigold, terracotta, brass"]
        assert out["delta_pct"] == 2.5
        assert out["highlight"] is True
        assert "unwanted" not in out
