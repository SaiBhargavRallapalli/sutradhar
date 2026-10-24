"""Currency-aware money helpers.

Sutradhar stores every amount as an **integer number of minor units** (paise,
cents, yen ...). Integers avoid the classic floating-point budget bug where a
column of rounded floats fails to add up to the stated total, and they let the
budget allocator guarantee ``sum(line items) == total`` exactly. See
``sutradhar.engines.budget``.

Formatting supports Indian lakh/crore grouping for INR, which matters because
many of the weddings this planner targets are budgeted in that system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum

__all__ = [
    "Currency",
    "CurrencyInfo",
    "to_minor",
    "from_minor",
    "format_minor",
    "parse_amount",
    "format_amount",
]


@dataclass(frozen=True, slots=True)
class CurrencyInfo:
    """Static metadata about a currency."""

    code: str
    symbol: str
    decimals: int
    indian_grouping: bool = False


class Currency(str, Enum):
    """Currencies Sutradhar can plan in."""

    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    AED = "AED"
    SGD = "SGD"
    AUD = "AUD"
    JPY = "JPY"

    @property
    def info(self) -> CurrencyInfo:
        return _INFO[self.value]

    @classmethod
    def parse(cls, value: str) -> "Currency":
        """Parse a currency, accepting codes *and* symbols, case-insensitively.

        ``"INR"``, ``"inr"`` and ``"₹"`` all resolve to :attr:`Currency.INR`.
        This mirrors the ``parse`` classmethod every enum in
        :mod:`sutradhar.enums` provides, which is what
        :func:`sutradhar.inputs._parse_enum` relies on. It lives here rather
        than inheriting the shared mixin so that ``money.py`` stays free of
        imports and can never introduce a cycle.
        """
        raw = value.strip()
        folded = raw.upper()
        for member in cls:
            if folded == member.value.upper():
                return member
        for member in cls:
            if raw == member.info.symbol.strip():
                return member
        options = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown currency {value!r}; expected one of: {options}")


_INFO: dict[str, CurrencyInfo] = {
    "INR": CurrencyInfo("INR", "₹", 2, indian_grouping=True),
    "USD": CurrencyInfo("USD", "$", 2),
    "EUR": CurrencyInfo("EUR", "€", 2),
    "GBP": CurrencyInfo("GBP", "£", 2),
    "AED": CurrencyInfo("AED", "AED ", 2),
    "SGD": CurrencyInfo("SGD", "S$", 2),
    "AUD": CurrencyInfo("AUD", "A$", 2),
    # JPY has no minor unit; 1 yen is the smallest coin.
    "JPY": CurrencyInfo("JPY", "¥", 0),
}


def _factor(currency: Currency) -> int:
    return 10 ** currency.info.decimals


def to_minor(amount: float | int | str | Decimal, currency: Currency) -> int:
    """Convert a major-unit amount into an integer count of minor units.

    Parsing goes through :class:`~decimal.Decimal` so ``"12.34"`` and ``12.34``
    produce the identical integer, and the result is half-up rounded which is
    the convention people expect from a bank or a bill.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"cannot parse amount {amount!r}") from exc
    return int((value * _factor(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: Currency) -> float:
    """Convert minor units back to a major-unit float (for display/maths)."""
    return round(minor / _factor(currency), currency.info.decimals)


def _group(digits: str, indian: bool) -> str:
    """Group a run of digits for display.

    Indian grouping is ``1,23,45,678`` (a leading group of three, then twos);
    the international default is groups of three (``12,345,678``).
    """
    if len(digits) <= 3:
        return digits
    if not indian:
        head, tail = digits[:-3], digits[-3:]
        out = tail
        while len(head) > 3:
            out = f"{head[-3:]},{out}"
            head = head[:-3]
        return f"{head},{out}" if head else out
    # Indian: last three, then twos.
    head, tail = digits[:-3], digits[-3:]
    out = tail
    while len(head) > 2:
        out = f"{head[-2:]},{out}"
        head = head[:-2]
    return f"{head},{out}" if head else out


def format_minor(minor: int, currency: Currency, *, with_symbol: bool = True) -> str:
    """Format minor units as a human-readable currency string.

    ``format_minor(12345678, Currency.INR)`` -> ``"₹1,23,456.78"``.
    """
    info = currency.info
    sign = "-" if minor < 0 else ""
    prefix = info.symbol if with_symbol else ""
    if info.decimals == 0:
        # Zero-decimal currencies (JPY) have no fractional part to render.
        return f"{sign}{prefix}{_group(str(abs(minor)), info.indian_grouping)}"
    scale = _factor(currency)
    major, frac = divmod(abs(minor), scale)
    body = f"{_group(str(major), info.indian_grouping)}.{frac:0{info.decimals}d}"
    return f"{sign}{prefix}{body}"


def format_amount(amount: float | int | str, currency: Currency, *, with_symbol: bool = True) -> str:
    """Convenience wrapper: parse a major-unit amount and format it."""
    return format_minor(to_minor(amount, currency), currency, with_symbol=with_symbol)


def parse_amount(text: str, currency: Currency) -> int:
    """Parse a user-typed amount into minor units.

    Tolerates symbols, thousands separators, Indian lakh grouping and the
    suffixes ``k`` (thousand) and ``L``/``lac``/``lakh``. Raises ``ValueError``
    with a helpful message when nothing numeric can be recovered.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("amount is empty")

    cleaned = raw.replace(",", "").replace(" ", "")
    for symbol in (currency.info.symbol.strip(), currency.value):
        cleaned = cleaned.replace(symbol, "")

    multiplier = 1
    lowered = cleaned.lower()
    # Ordered longest-first: "lakh" must be tried before "l", otherwise
    # "15lakh" would match the bare "l" and leave "ak" behind.
    for suffix, factor in (
        ("lakh", 100_000),
        ("lac", 100_000),
        ("crore", 10_000_000),
        ("cr", 10_000_000),
        ("k", 1_000),
        ("m", 1_000_000),
        ("bn", 1_000_000_000),
        ("l", 100_000),  # "15L" is the everyday Indian shorthand for 15 lakh
    ):
        if lowered.endswith(suffix):
            multiplier = factor
            cleaned = cleaned[: -len(suffix)]
            break

    # Allow an explicit leading "+" but reject a negative amount outright:
    # lstrip("+-") would have silently turned "-500" into a positive 500.
    cleaned = cleaned.lstrip("+")
    if cleaned.startswith("-"):
        raise ValueError(f"{text!r} is negative; a budget or quote cannot be less than zero")
    if not cleaned or not cleaned.replace(".", "", 1).isdigit():
        raise ValueError(f"cannot read {text!r} as an amount of {currency.value}")

    try:
        return to_minor(float(cleaned) * multiplier, currency)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"cannot read {text!r} as an amount of {currency.value}") from exc
