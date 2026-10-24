"""Catalog registry - the single lookup point for event-type knowledge.

Engines never import a vertical module directly; they ask for a
:class:`~sutradhar.catalog.specs.EventTypeProfile` by
:class:`~sutradhar.enums.EventType`. That indirection is what makes adding a
vertical a one-file change.
"""

from __future__ import annotations

from ..enums import EventType, Season
from ..exceptions import CatalogError
from .anniversary import ANNIVERSARY
from .birthday import BIRTHDAY
from .community import COMMUNITY
from .housewarming import HOUSEWARMING
from .specs import EventTypeProfile
from .wedding import WEDDING

__all__ = [
    "PROFILES",
    "get_profile",
    "list_profiles",
    "season_for",
    "season_multiplier",
]

#: Every vertical Sutradhar ships with, keyed by event type.
PROFILES: dict[EventType, EventTypeProfile] = {
    EventType.WEDDING: WEDDING,
    EventType.BIRTHDAY: BIRTHDAY,
    EventType.COMMUNITY: COMMUNITY,
    EventType.HOUSEWARMING: HOUSEWARMING,
    EventType.ANNIVERSARY: ANNIVERSARY,
}


def get_profile(event_type: EventType) -> EventTypeProfile:
    """Return the profile for ``event_type`` or raise :class:`CatalogError`."""
    try:
        return PROFILES[event_type]
    except KeyError as exc:  # pragma: no cover - defensive, PROFILES covers every member
        raise CatalogError(f"no catalog profile registered for {event_type!r}") from exc


def list_profiles() -> list[EventTypeProfile]:
    """Return every registered profile, ordered for display."""
    return sorted(PROFILES.values(), key=lambda p: p.event_type.value)


def season_for(event_type: EventType, month: int, *, override: Season | None = None) -> Season:
    """Classify demand for a given calendar month.

    ``month`` is 1-12. An explicit ``override`` always wins, so a host who knows
    their own market better than the catalogue can say so.
    """
    if override is not None:
        return override
    profile = get_profile(event_type)
    if month in profile.peak_months:
        return Season.PEAK
    if month in profile.shoulder_months:
        return Season.SHOULDER
    return Season.OFF_PEAK


#: How much overall price pressure each season carries. Peak months for a
#: wedding in India are genuinely 15-25% more expensive for venue and decor.
_SEASON_MULTIPLIER: dict[Season, float] = {
    Season.PEAK: 1.18,
    Season.SHOULDER: 1.06,
    Season.OFF_PEAK: 0.95,
}


def season_multiplier(season: Season) -> float:
    """Global price index for a season; per-category elasticity refines it."""
    return _SEASON_MULTIPLIER[season]
