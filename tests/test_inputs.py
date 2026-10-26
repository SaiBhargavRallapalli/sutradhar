"""Input-boundary tests: untrusted mappings, aliases and validation errors.

Every other module trusts a validated ``Brief``; this is the one place that has
to cope with whatever JSON, a form or a model actually produced. The tests are
grouped by concern - dates, money, enums, taste, limits - so a failure names the
boundary that broke.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from sutradhar.enums import CityTier, EventType, Priority, Season, WindExposure
from sutradhar.exceptions import ValidationError
from sutradhar.inputs import brief_from_dict, brief_from_json
from sutradhar.models import MAX_GUESTS
from sutradhar.money import Currency


def payload(**overrides: Any) -> dict[str, Any]:
    """A minimal valid brief, with per-test overrides applied."""
    base: dict[str, Any] = {
        "event_type": "wedding",
        "event_date": "2027-06-06",
        "guest_count": 120,
        "budget": "15 lakh",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Defaults and aliases
# --------------------------------------------------------------------------- #
def test_minimal_brief_fills_in_documented_defaults() -> None:
    brief = brief_from_dict(payload())

    assert brief.event_type is EventType.WEDDING
    assert brief.event_date == date(2027, 6, 6)
    assert brief.guest_count == 120
    assert brief.budget_total_minor == 150_000_000  # 15 lakh, in paise
    assert brief.currency is Currency.INR
    assert brief.city_tier is CityTier.TIER_1
    assert brief.setting is WindExposure.INDOOR
    assert brief.season_override is None
    assert brief.location == "" and brief.title == "" and brief.host_name == ""
    assert brief.priorities == {}
    assert brief.taste.is_empty


def test_lenient_aliases_are_accepted() -> None:
    brief = brief_from_dict(
        {
            "type": "birthday",
            "date": "2027-01-01",
            "headcount": "8",
            "total": "5,000",
            "host": "  Asha  ",
            "indoor_outdoor": "open_air",
            "taste": {"style": "garden", "accessibility": "step-free"},
        }
    )

    assert brief.event_type is EventType.BIRTHDAY
    assert brief.guest_count == 8
    assert brief.budget_total_minor == 500_000
    assert brief.host_name == "Asha"
    assert brief.setting is WindExposure.OPEN_AIR
    assert brief.taste.style_keywords == ("garden",)
    assert brief.taste.accessibility_notes == ("step-free",)


def test_explicit_minor_units_win_over_the_major_unit_alias() -> None:
    brief = brief_from_dict(payload(budget_total_minor=123_456, budget="999"))
    assert brief.budget_total_minor == 123_456


def test_numeric_budget_is_treated_as_major_units() -> None:
    brief = brief_from_dict(payload(budget=5_000))
    assert brief.budget_total_minor == 500_000


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "2027-06-06",
        "06/06/2027",
        "06-06-2027",
        "6 Jun 2027",
        "06 June 2027",
        "Jun 06 2027",
        "June 06, 2027",
    ],
)
def test_common_written_dates_parse(text: str) -> None:
    assert brief_from_dict(payload(event_date=text)).event_date == date(2027, 6, 6)


def test_date_objects_are_accepted_directly() -> None:
    assert brief_from_dict(payload(event_date=date(2027, 6, 6))).event_date == date(2027, 6, 6)
    stamp = datetime(2027, 6, 6, 18, 30, tzinfo=timezone.utc)
    assert brief_from_dict(payload(event_date=stamp)).event_date == date(2027, 6, 6)


def test_numeric_dates_are_epoch_seconds_or_milliseconds() -> None:
    seconds = int(datetime(2027, 6, 6, tzinfo=timezone.utc).timestamp())

    assert brief_from_dict(payload(event_date=seconds)).event_date == date(2027, 6, 6)
    assert brief_from_dict(payload(event_date=seconds * 1000)).event_date == date(2027, 6, 6)


def test_missing_date_names_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(event_date=None))
    assert caught.value.field == "event_date"


def test_unreadable_date_is_rejected_with_guidance() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(event_date="sometime in June"))
    assert caught.value.field == "event_date"
    assert "YYYY-MM-DD" in caught.value.message


# --------------------------------------------------------------------------- #
# Money and guests
# --------------------------------------------------------------------------- #
def test_budget_strings_understand_indian_grouping_and_suffixes() -> None:
    assert brief_from_dict(payload(budget="15 lakh")).budget_total_minor == 150_000_000
    assert brief_from_dict(payload(budget="₹15,00,000")).budget_total_minor == 150_000_000
    assert brief_from_dict(payload(budget="2 crore")).budget_total_minor == 2_000_000_000


def test_missing_budget_names_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(budget=None))
    assert caught.value.field == "budget"


def test_unparseable_budget_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(budget="a lot"))
    assert caught.value.field == "budget"


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(budget=0))
    assert caught.value.field == "budget_total_minor"


def test_guest_count_must_be_a_whole_number() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(guest_count="a few"))
    assert caught.value.field == "guest_count"


def test_guest_count_upper_bound_is_enforced() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(guest_count=MAX_GUESTS + 1))
    assert caught.value.field == "guest_count"


# --------------------------------------------------------------------------- #
# Enums, priorities and season
# --------------------------------------------------------------------------- #
def test_event_type_is_required() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(event_type=None))
    assert caught.value.field == "event_type"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "festival"),
        ("currency", "XYZ"),
        ("city_tier", "tier_9"),
        ("setting", "underwater"),
        ("season", "monsoon"),
    ],
)
def test_bad_enum_values_name_the_offending_field(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(**{field: value}))
    assert caught.value.field == field


def test_priorities_are_normalised_to_lower_case_keys() -> None:
    brief = brief_from_dict(
        payload(priorities={" Catering ": "HIGH", "decor": "low", "photography": "vital"})
    )
    assert brief.priorities == {
        "catering": Priority.HIGH,
        "decor": Priority.LOW,
        "photography": Priority.VITAL,
    }


def test_priorities_must_be_a_mapping() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(priorities="catering high"))
    assert caught.value.field == "priorities"


def test_season_override_is_optional() -> None:
    assert brief_from_dict(payload()).season_override is None
    assert brief_from_dict(payload(season="peak")).season_override is Season.PEAK


# --------------------------------------------------------------------------- #
# Taste profile
# --------------------------------------------------------------------------- #
def test_taste_accepts_comma_strings_lists_and_scalars() -> None:
    brief = brief_from_dict(
        payload(
            taste={
                "palette": "sage, ivory",
                "style_keywords": ["minimal", "  garden  ", ""],
                "dietary": "vegan",
                "vibe": "  unhurried  ",
            }
        )
    )
    assert brief.taste.palette == ("sage", "ivory")
    assert brief.taste.style_keywords == ("minimal", "garden")
    assert brief.taste.dietary == ("vegan",)
    assert brief.taste.vibe == "unhurried"


def test_taste_must_be_a_mapping() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(taste=["garden"]))
    assert caught.value.field == "taste"


# --------------------------------------------------------------------------- #
# Length limits and JSON entry points
# --------------------------------------------------------------------------- #
def test_over_long_text_is_truncated_rather_than_rejected() -> None:
    brief = brief_from_dict(
        payload(
            title="t" * 500,
            location="l" * 500,
            host_name="h" * 500,
            notes="n" * 3000,
            natural_language="q" * 9000,
        )
    )
    assert len(brief.title) == 120
    assert len(brief.location) == 120
    assert len(brief.host_name) == 80
    assert len(brief.notes) == 2_000
    assert len(brief.natural_language) == 4_000


def test_json_error_reports_the_line_number() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_json('{"event_type": "wedding",}')
    assert caught.value.field == "json"
    assert "line 1" in caught.value.message


def test_json_must_be_an_object() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_json("[]")
    assert caught.value.field == "brief"


def test_a_valid_json_document_round_trips() -> None:
    import json

    brief = brief_from_json(json.dumps(payload()))
    assert brief.event_type is EventType.WEDDING
    assert brief.event_date == date(2027, 6, 6)


def test_blank_date_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(event_date="   "))
    assert caught.value.field == "event_date"
    assert "required" in caught.value.message


def test_a_scalar_taste_value_becomes_a_single_entry() -> None:
    brief = brief_from_dict(payload(taste={"dietary": 42}))
    assert brief.taste.dietary == ("42",)


def test_an_absurd_timestamp_is_rejected_rather_than_crashing() -> None:
    with pytest.raises(ValidationError) as caught:
        brief_from_dict(payload(event_date=10**20))
    assert caught.value.field == "event_date"
    assert "out of range" in caught.value.message
