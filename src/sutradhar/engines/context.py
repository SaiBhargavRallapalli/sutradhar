"""Planning context computed once per plan and threaded through the engines.

Deriving these figures in one place means every engine agrees on what "peak
season" or "how far away is the event" means, and gives the tests a single
seam to control time from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..catalog.registry import season_multiplier
from ..enums import CityTier, EventType, Season, WindExposure
from ..models import Brief

__all__ = ["TIER_INDEX", "CostContext", "cost_context"]

#: Rough relative cost-of-living index by city tier, anchored at tier-1 = 1.0.
#: Used for advice and for re-weighting the categories that genuinely get
#: pricier in expensive metros (rent, hospitality, city logistics).
TIER_INDEX: dict[CityTier, float] = {
    CityTier.TIER_1: 1.0,
    CityTier.TIER_2: 0.86,
    CityTier.TIER_3: 0.72,
    CityTier.GLOBAL: 1.45,
}


@dataclass(frozen=True, slots=True)
class CostContext:
    """The derived facts every engine needs about one brief."""

    event_type: EventType
    guests: int
    season: Season
    lead_days: int
    tier_index: float
    season_price_index: float
    currency_symbol: str
    currency_code: str
    setting: WindExposure
    event_date: date
    today: date

    @property
    def is_outdoor(self) -> bool:
        return self.setting is not WindExposure.INDOOR


def cost_context(brief: Brief, season: Season, *, today: date) -> CostContext:
    """Build the :class:`CostContext` for ``brief`` as of ``today``.

    ``today`` is always injected rather than read from the clock inside the
    engine, so a plan is reproducible and the test suite can simulate
    "planning six weeks out" without monkeypatching.
    """
    return CostContext(
        event_type=brief.event_type,
        guests=max(1, brief.guest_count),
        season=season,
        lead_days=(brief.event_date - today).days,
        tier_index=TIER_INDEX[brief.city_tier],
        season_price_index=season_multiplier(season),
        currency_symbol=brief.currency.info.symbol.strip(),
        currency_code=brief.currency.value,
        setting=brief.setting,
        event_date=brief.event_date,
        today=today,
    )
