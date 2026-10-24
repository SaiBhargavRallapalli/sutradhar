"""Money tests.

Money is the one place where a small bug has real consequences, so these tests
are deliberately fussy: exact integer arithmetic, correct Indian grouping, and
parsing that is forgiving about how humans actually type amounts.
"""
from __future__ import annotations

import pytest

from sutradhar.money import Currency, format_minor, from_minor, parse_amount, to_minor


class TestCurrencyParsing:
    @pytest.mark.parametrize("value", ["INR", "inr", "Inr", "₹"])
    def test_parses_code_and_symbol(self, value: str) -> None:
        assert Currency.parse(value) is Currency.INR

    @pytest.mark.parametrize("raw,expected", [
        ("USD", Currency.USD), ("usd", Currency.USD), ("$", Currency.USD),
        ("EUR", Currency.EUR), ("€", Currency.EUR),
        ("GBP", Currency.GBP), ("£", Currency.GBP),
        ("JPY", Currency.JPY), ("¥", Currency.JPY),
        ("SGD", Currency.SGD), ("S$", Currency.SGD),
    ])
    def test_parses_every_supported_currency(self, raw: str, expected: Currency) -> None:
        assert Currency.parse(raw) is expected

    def test_rejects_unknown_currency_with_a_helpful_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            Currency.parse("XYZ")
        assert "XYZ" in str(exc.value)
        assert "INR" in str(exc.value), "the error should list valid options"


class TestConversion:
    def test_to_minor_is_exact_for_strings(self) -> None:
        assert to_minor("12.34", Currency.INR) == 1234
        assert to_minor("12.3", Currency.INR) == 1230
        assert to_minor("12", Currency.INR) == 1200

    def test_to_minor_matches_for_float_and_string(self) -> None:
        """The classic float bug: 0.1 + 0.2 style drift must not appear."""
        assert to_minor(0.1, Currency.USD) == to_minor("0.1", Currency.USD)
        assert to_minor(19.99, Currency.USD) == 1999

    def test_half_up_rounding_matches_human_expectation(self) -> None:
        assert to_minor("10.005", Currency.INR) == 1001
        assert to_minor("10.004", Currency.INR) == 1000

    def test_zero_decimal_currency_has_no_fraction(self) -> None:
        assert to_minor(500, Currency.JPY) == 500
        assert to_minor("500", Currency.JPY) == 500

    def test_round_trip(self) -> None:
        for minor in (0, 1, 99, 100, 12345, 150000000):
            assert to_minor(from_minor(minor, Currency.INR), Currency.INR) == minor

    def test_from_minor_returns_a_float(self) -> None:
        assert from_minor(123456, Currency.INR) == 1234.56


class TestFormatting:
    def test_indian_grouping(self) -> None:
        assert format_minor(12345678, Currency.INR) == "₹1,23,456.78"
        assert format_minor(10000000, Currency.INR) == "₹1,00,000.00"
        assert format_minor(100000, Currency.INR) == "₹1,000.00"
        assert format_minor(1000, Currency.INR) == "₹10.00"

    def test_international_grouping(self) -> None:
        assert format_minor(12345678, Currency.USD) == "$123,456.78"
        assert format_minor(100000, Currency.USD) == "$1,000.00"

    def test_zero_decimal_currency_has_no_decimals_shown(self) -> None:
        assert format_minor(12345, Currency.JPY) == "¥12,345"

    def test_zero_amount(self) -> None:
        assert format_minor(0, Currency.INR) == "₹0.00"

    def test_negative_amount_keeps_its_sign(self) -> None:
        assert format_minor(-123456, Currency.INR).startswith("-")

    def test_symbol_can_be_suppressed(self) -> None:
        assert format_minor(123456, Currency.INR, with_symbol=False) == "1,234.56"

    def test_small_amounts_are_not_dropped(self) -> None:
        assert format_minor(1, Currency.INR) == "₹0.01"
        assert format_minor(5, Currency.USD) == "$0.05"

    def test_formatting_is_pure(self) -> None:
        """Formatting must never mutate anything - it is called in loops."""
        assert format_minor(123456, Currency.INR) == format_minor(123456, Currency.INR)


class TestParsing:
    @pytest.mark.parametrize("text,expected", [
        ("1500000", 150000000),
        ("₹15,00,000", 150000000),
        ("15 lakh", 150000000),
        ("15L", 150000000),
        ("1.5 crore", 1500000000),
        ("250000", 25000000),
        ("12.5k", 1250000),
    ])
    def test_parses_how_humans_type_money(self, text: str, expected: int) -> None:
        assert parse_amount(text, Currency.INR) == expected

    def test_parses_plain_dollar_amounts(self) -> None:
        assert parse_amount("$3,000", Currency.USD) == 300000
        assert parse_amount("3000", Currency.USD) == 300000
        assert parse_amount("3k", Currency.USD) == 300000

    def test_explicit_plus_sign_is_accepted(self) -> None:
        assert parse_amount("+500", Currency.INR) == 50000

    def test_negative_amount_is_rejected_not_silently_flipped(self) -> None:
        """A leading minus must never parse as a positive amount."""
        with pytest.raises(ValueError):
            parse_amount("-500", Currency.INR)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_amount("   ", Currency.INR)

    def test_garbage_is_rejected(self) -> None:
        for bad in ("abc", "₹", "...", "lakh"):
            with pytest.raises(ValueError):
                parse_amount(bad, Currency.INR)

    def test_error_message_names_the_currency(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_amount("abc", Currency.INR)
        assert "INR" in str(exc.value)

    def test_parse_then_format_round_trips(self) -> None:
        for text in ("1500000", "₹15,00,000", "15 lakh"):
            assert from_minor(parse_amount(text, Currency.INR), Currency.INR) == 1500000.0

    def test_lakh_shorthand_agrees_with_the_spelled_form(self) -> None:
        assert parse_amount("15L", Currency.INR) == parse_amount("15 lakh", Currency.INR)
        assert parse_amount("2l", Currency.INR) == parse_amount("2 lakh", Currency.INR)


class TestNoFloatDrift:
    def test_column_of_amounts_still_adds_up(self) -> None:
        """The bug this architecture exists to prevent: a budget column that
        does not reconcile because of float rounding."""
        amounts = [to_minor(x, Currency.INR) for x in
                   ("4194.24", "2572.70", "1483.08", "1295.08", "1117.93")]
        assert sum(amounts) == 419424 + 257270 + 148308 + 129508 + 111793

    def test_integer_addition_is_exact_where_floats_are_not(self) -> None:
        # 0.1 + 0.2 != 0.3 in binary floating point; in minor units it does.
        assert to_minor("0.1", Currency.USD) + to_minor("0.2", Currency.USD) == to_minor("0.3", Currency.USD)
