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

The package has **zero runtime dependencies** - it runs on the Python standard
library alone, offline, with no API key.
"""

from __future__ import annotations

from .enums import CityTier, EventType, Priority, RiskLevel, Season, WindExposure
from .models import (
    BudgetPlan,
    Brief,
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
    "__version__",
    "EventType",
    "CityTier",
    "Priority",
    "RiskLevel",
    "Season",
    "WindExposure",
    "Currency",
    "TasteProfile",
    "Brief",
    "Plan",
    "BudgetPlan",
    "TimelinePlan",
    "VendorPlan",
    "RiskPlan",
    "IdeaConcept",
    "Planner",
    "plan_event",
]
