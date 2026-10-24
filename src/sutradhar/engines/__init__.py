"""Deterministic planning engines.

Each engine takes a validated brief plus catalogue knowledge and returns a
domain model. They are pure functions of their inputs - no clock reads, no
global state, no I/O - which is what makes the whole planner reproducible and
trivially testable.
"""

from __future__ import annotations

from .budget import allocate_budget
from .personalize import catalogue_ideas, ground_concepts
from .risk import build_risks, risk_warnings
from .timeline import build_timeline
from .vendors import build_vendors

__all__ = [
    "allocate_budget",
    "build_timeline",
    "build_vendors",
    "build_risks",
    "risk_warnings",
    "catalogue_ideas",
    "ground_concepts",
]
