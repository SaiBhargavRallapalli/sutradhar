"""Shared fixtures.

Every test that touches planning passes an explicit ``today`` so results are
byte-for-byte reproducible; nothing in this suite should depend on the day it
happens to run.
"""
from __future__ import annotations

from datetime import date

import pytest

from sutradhar import Brief, CityTier, Currency, EventType, Priority, WindExposure
from sutradhar.config import Settings
from sutradhar.models import Plan

#: A fixed reference date; 2026-01-01 is a Thursday, which keeps weekday maths
#: in the exports predictable.
TODAY = date(2026, 1, 1)
EVENT = date(2027, 6, 6)  # a Sunday, ~17 months out


@pytest.fixture
def today() -> date:
    return TODAY


@pytest.fixture
def offline_settings() -> Settings:
    """Settings with the model layer switched off.

    The catalogue path is the default for tests: it is deterministic, needs no
    network and no key, and exercises the same pipeline code a live run does.
    """
    return Settings(ai_enabled=False, provider="mock")


@pytest.fixture
def wedding_brief() -> Brief:
    return Brief(
        event_type=EventType.WEDDING,
        event_date=EVENT,
        guest_count=250,
        budget_total_minor=1_500_000_00,
        currency=Currency.INR,
        city_tier=CityTier.TIER_2,
        location="Pune",
        title="Aarav & Meera",
        host_name="The Raghunath family",
        setting=WindExposure.OPEN_AIR,
        priorities={"catering": Priority.HIGH, "photography": Priority.HIGH},
        notes="Courtyard under old trees rather than a banquet hall.",
    )


@pytest.fixture
def tiny_brief() -> Brief:
    """A deliberately cramped plan: 40 days of runway for a wedding."""
    return Brief(
        event_type=EventType.WEDDING,
        event_date=date(2026, 2, 10),
        guest_count=60,
        budget_total_minor=3_00_000_00,
        currency=Currency.INR,
        city_tier=CityTier.TIER_3,
        location="Nashik",
        setting=WindExposure.INDOOR,
    )


@pytest.fixture
def plan(wedding_brief: Brief, offline_settings: Settings, today: date) -> Plan:
    from sutradhar.pipeline import plan_event

    return plan_event(wedding_brief, settings=offline_settings, today=today)
