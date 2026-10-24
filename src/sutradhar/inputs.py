"""Turning untrusted input (JSON, CLI args, web forms) into a validated Brief.

Every entry point funnels through :func:`brief_from_dict`, so validation and
coercion happen in exactly one place. Errors carry the offending field name, so
the CLI can print "budget_total: must be greater than zero" rather than a
traceback - and the web layer can map it straight onto a form control.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Mapping

from .enums import CityTier, EventType, Priority, Season, WindExposure
from .exceptions import ValidationError
from .models import Brief, TasteProfile
from .money import Currency, parse_amount

__all__ = ["brief_from_dict", "brief_from_json"]

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d, %Y")


def _parse_date(value: Any, field: str) -> date:
    """Accept an ISO date, several common written forms, or a ``datetime``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # A timestamp in milliseconds or seconds.
        seconds = float(value) / 1000 if value > 1e11 else float(value)
        return datetime.utcfromtimestamp(seconds).date()  # noqa: DTZ003 - caller-supplied epoch
    text = str(value or "").strip()
    if not text:
        raise ValidationError(field, "a date is required (YYYY-MM-DD)")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise ValidationError(
            field, f"could not read {text!r} as a date; try YYYY-MM-DD"
        ) from exc


def _parse_enum(enum_cls: Any, value: Any, field: str) -> Any:
    try:
        return enum_cls.parse(str(value))
    except ValueError as exc:
        raise ValidationError(field, str(exc)) from exc


def _parse_taste(raw: Any) -> TasteProfile:
    """Coerce a taste mapping into a :class:`TasteProfile`.

    Tolerates both a list of strings and a single comma-separated string, since
    form submissions and CLI flags arrive in different shapes.
    """
    if raw is None:
        return TasteProfile()
    if not isinstance(raw, Mapping):
        raise ValidationError("taste", "must be an object of preference lists")

    def as_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            parts = [str(part).strip() for part in value]
        else:
            parts = [str(value).strip()]
        return tuple(part for part in parts if part)

    return TasteProfile(
        palette=as_tuple(raw.get("palette")),
        style_keywords=as_tuple(raw.get("style_keywords") or raw.get("style")),
        traditions=as_tuple(raw.get("traditions")),
        vibe=str(raw.get("vibe") or "").strip(),
        dietary=as_tuple(raw.get("dietary")),
        accessibility_notes=as_tuple(raw.get("accessibility_notes") or raw.get("accessibility")),
        must_haves=as_tuple(raw.get("must_haves")),
        avoid=as_tuple(raw.get("avoid")),
    )


def _parse_priorities(raw: Any) -> dict[str, Priority]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationError("priorities", "must map a budget category key to low/normal/high/vital")
    out: dict[str, Priority] = {}
    for key, value in raw.items():
        out[str(key).strip().lower()] = _parse_enum(Priority, value, f"priorities.{key}")
    return out


def brief_from_dict(data: Mapping[str, Any]) -> Brief:
    """Build a validated :class:`Brief` from a plain mapping.

    Accepts lenient aliases (``budget``/``budget_total``/``total``,
    ``guests``/``guest_count``/``headcount``) so that JSON written by a human,
    a form, or a model all parse the same way.
    """
    if not isinstance(data, Mapping):
        raise ValidationError("brief", "expected an object describing the event")

    event_type = _parse_enum(EventType, data.get("event_type") or data.get("type"), "event_type")
    event_date = _parse_date(
        data.get("event_date") or data.get("date"), "event_date"
    )

    guests_raw = data.get("guest_count", data.get("guests", data.get("headcount")))
    try:
        guest_count = int(guests_raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("guest_count", "guest count must be a whole number") from exc

    currency = _parse_enum(
        Currency, data.get("currency") or data.get("currency_code") or "INR", "currency"
    )

    budget_raw = data.get("budget_total_minor", data.get("budget", data.get("budget_total", data.get("total"))))
    if budget_raw is None:
        raise ValidationError("budget", "a total budget is required")
    try:
        budget_minor = (
            int(budget_raw)
            if data.get("budget_total_minor") is not None and not isinstance(budget_raw, str)
            else parse_amount(str(budget_raw), currency)
        )
    except ValueError as exc:
        raise ValidationError("budget", str(exc)) from exc

    setting = _parse_enum(
        WindExposure,
        data.get("setting") or data.get("indoor_outdoor") or WindExposure.INDOOR.value,
        "setting",
    )

    season_override = None
    if data.get("season"):
        season_override = _parse_enum(Season, data["season"], "season")

    brief = Brief(
        event_type=event_type,
        event_date=event_date,
        guest_count=guest_count,
        budget_total_minor=budget_minor,
        currency=currency,
        city_tier=_parse_enum(CityTier, data.get("city_tier") or CityTier.TIER_1.value, "city_tier"),
        location=str(data.get("location") or "").strip()[:120],
        title=str(data.get("title") or "").strip()[:120],
        host_name=str(data.get("host_name") or data.get("host") or "").strip()[:80],
        setting=setting,
        season_override=season_override,
        priorities=_parse_priorities(data.get("priorities")),
        taste=_parse_taste(data.get("taste")),
        notes=str(data.get("notes") or "").strip()[:2000],
        natural_language=str(data.get("natural_language") or data.get("request") or "").strip()[:4000],
    )
    brief.validate()
    return brief


def brief_from_json(text: str) -> Brief:
    """Parse a JSON document into a validated :class:`Brief`."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError("json", f"not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    return brief_from_dict(payload)
