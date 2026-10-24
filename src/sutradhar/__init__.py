"""Sutradhar - a GenAI celebration & event planning co-pilot.

Sutradhar plans weddings, birthdays, community gatherings, housewarmings and
anniversaries. It combines **deterministic planning engines** (a bounded budget
allocator, a critical-path scheduler, vendor and risk checklists) with a
**pluggable large-language-model layer** for personalisation and natural-language
briefs.

The split is deliberate and is the core architectural idea:

* Numbers come from code, so they are exact, reproducible and testable.
* Words come from the model, so they are warm, specific and personal.
* Everything the model returns is validated against a schema and grounded
  against the engine's numbers before it reaches you.

The planning core uses only the Python standard library and works offline
without an API key. The web command adds one pinned production-server
dependency: Waitress.
"""

from __future__ import annotations

from .enums import CityTier, EventType, Priority, RiskLevel, Season, WindExposure
from .models import (
    Brief,
    BudgetPlan,
    IdeaConcept,
    Plan,
    RiskPlan,
    TasteProfile,
    TimelinePlan,
    VendorPlan,
)
from .money import Currency
from .pipeline import Planner, plan_event

__version__ = "1.0.0"

__all__ = [
    "Brief",
    "BudgetPlan",
    "CityTier",
    "Currency",
    "EventType",
    "IdeaConcept",
    "Plan",
    "Planner",
    "Priority",
    "RiskLevel",
    "RiskPlan",
    "Season",
    "TasteProfile",
    "TimelinePlan",
    "VendorPlan",
    "WindExposure",
    "__version__",
    "plan_event",
]
