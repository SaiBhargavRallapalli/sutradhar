"""Recursive conversion of domain objects into plain JSON-ready structures.

Kept deliberately generic so that ``to_dict`` on a model never has to enumerate
its own field types - drop a new enum or nested dataclass into a model and it
serialises for free.

Derived values
--------------
Dataclasses serialise their *fields*. Most of the interesting numbers in
Sutradhar, though, are derived properties (``BudgetPlan.contingency_pct``,
``RiskItem.score``, ``Task.days_before_event``), and a naive field-only
serialiser silently drops them - which is exactly the bug this module exists
to prevent.

A model therefore opts in explicitly::

    @dataclass(frozen=True, slots=True)
    class BudgetPlan:
        ...
        _COMPUTED: tuple[str, ...] = ("contingency_pct", "unallocated_minor")

Opting in - rather than serialising every property automatically - keeps the
JSON contract deliberate: adding a convenience property to a model never
quietly changes the public API shape that the web UI and exporters depend on.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = ["dumps", "to_plain", "with_computed"]


def _is_dataclass_instance(obj: Any) -> bool:
    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)


def _fields_of(obj: Any) -> dict[str, Any]:
    """Field values of ``obj``, each routed back through :func:`to_plain`."""
    return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.init}


def with_computed(obj: Any) -> dict[str, Any]:
    """``to_plain(obj)`` plus the derived properties ``obj`` declared.

    Returns a flat dict so a caller can spread or override keys. Anything that
    is not a dataclass comes back as a single-key mapping, which keeps this
    safe to call on any value.
    """
    if not _is_dataclass_instance(obj):
        return {"value": to_plain(obj)}
    out = _fields_of(obj)
    for name in getattr(type(obj), "_COMPUTED", ()):
        # A computed property that raises would break every export, so a bad
        # name is skipped rather than propagated.
        try:
            out[name] = to_plain(getattr(obj, name))
        except Exception:  # pragma: no cover - defensive
            continue
    return out


def to_plain(obj: Any) -> Any:
    """Recursively convert ``obj`` into JSON-serialisable primitives.

    Handles dataclasses, enums, dates/datetimes, decimals, paths and the
    standard containers. Unknown objects fall back to ``str(obj)`` so a new
    field type can never crash an export.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    if _is_dataclass_instance(obj):
        # Nested models go through `with_computed` too, so a derived value on a
        # child (RiskItem.score, BudgetLine.amount) survives to the top level.
        return with_computed(obj)
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(v) for v in obj]
    return str(obj)


def dumps(obj: Any, *, indent: int | None = 2) -> str:
    """Serialise ``obj`` (usually a :class:`~sutradhar.models.Plan`) to JSON."""
    return json.dumps(to_plain(obj), indent=indent, ensure_ascii=False, sort_keys=False)
