"""Personalisation engine - matching a host's taste to celebration concepts.

Works in two modes that produce the same shape of answer:

* **Catalogue mode (always available, offline).** Scores every curated concept
  against the host's taste profile using keyword-overlap similarity, then
  *grounds* the winner against the real budget - so an idea is never shown
  without knowing what it costs.
* **Model mode (optional).** The language model drafts concepts; this module
  still grounds them, and anything that does not fit the budget is either
  re-priced or flagged rather than silently accepted.

The grounding step is the point. An ungrounded idea is a wish; a grounded idea
is a plan.
"""

from __future__ import annotations

from ..catalog.specs import EventTypeProfile, ThemeSpec
from ..enums import RiskLevel, Source
from ..models import Brief, BudgetPlan, IdeaConcept, PlanWarning, TasteProfile

__all__ = ["catalogue_ideas", "concept_from_theme", "ground_concepts", "rank_themes"]

#: Concepts returned by default.
DEFAULT_IDEA_COUNT = 3


def _similarity(taste: TasteProfile, theme: ThemeSpec) -> float:
    """Jaccard-style overlap between a host's keywords and a theme's keywords.

    Deliberately simple and explainable: a host can be told *why* a concept was
    suggested ("you both matched on 'outdoor' and 'garden'").
    """
    host = taste.signal_tokens()
    if not host:
        return 0.0
    concept = {kw.lower() for kw in theme.style_keywords} | {c.lower() for c in theme.palette}
    if not concept:
        return 0.0
    overlap = host & concept
    if not overlap:
        return 0.0
    return len(overlap) / len(host | concept)


def _avoid_penalty(taste: TasteProfile, theme: ThemeSpec) -> float:
    """Penalise concepts that touch something the host explicitly wants to avoid.

    The avoided words are taken directly from ``taste.avoid``. Intersecting them
    with :meth:`TasteProfile.signal_tokens` first - as an earlier version did -
    silently disabled the feature, because ``signal_tokens`` deliberately omits
    the avoid group (a host listing "fireworks" to avoid them does not thereby
    signal an interest in fireworks).
    """
    avoid = {w.lower() for w in taste.avoid if w.strip()}
    if not avoid:
        return 0.0
    concept_text = " ".join(
        [theme.name, theme.tagline, *theme.style_keywords, *theme.palette]
    ).lower()
    hits = sum(1 for word in avoid if word in concept_text)
    return min(0.5, hits * 0.2)


def rank_themes(
    taste: TasteProfile,
    profile: EventTypeProfile,
    *,
    limit: int = DEFAULT_IDEA_COUNT,
) -> list[tuple[ThemeSpec, float, set[str]]]:
    """Rank the catalogue's themes for this host.

    Returns ``(theme, score, matched_keywords)`` triples so the interface can
    explain each suggestion rather than just asserting it is "for you".
    """
    scored: list[tuple[ThemeSpec, float, set[str]]] = []
    for theme in profile.themes:
        matched = taste.signal_tokens() & (
            {kw.lower() for kw in theme.style_keywords} | {c.lower() for c in theme.palette}
        )
        score = _similarity(taste, theme) - _avoid_penalty(taste, theme)
        scored.append((theme, score, matched))

    # Highest score first; cost and then name break ties so the order is stable
    # across runs. When every theme scores 0 (a host who gave only avoids, say),
    # this naturally degenerates into cheapest-first - which is the desired
    # fallback, and unlike a separate "all non-positive" branch it still lets a
    # penalty sink a theme the host explicitly asked to avoid.
    scored.sort(key=lambda item: (-item[1], item[0].delta_pct, item[0].name))
    return scored[:limit]


def concept_from_theme(
    theme: ThemeSpec, budget: BudgetPlan, *, matched: set[str] | None = None
) -> IdeaConcept:
    """Turn a catalogue theme into a budget-grounded :class:`IdeaConcept`."""
    delta_minor = round(budget.total_minor * theme.delta_pct / 100.0)
    fits, fit_note = _cost_fit(theme, delta_minor, budget)

    rationale = (
        "Suggested because it matched your " + ", ".join(sorted(matched)) + "."
        if matched
        else "A curated concept that suits this event type and budget."
    )

    return IdeaConcept(
        key=theme.key,
        name=theme.name,
        tagline=theme.tagline,
        palette=theme.palette,
        decor=theme.decor,
        menu=theme.menu,
        favors=theme.favors,
        attire=theme.attire,
        music=theme.music,
        activities=theme.activities,
        estimated_delta_pct=theme.delta_pct,
        cost_fit=fit_note,
        source=Source.CATALOG,
        grounded=fits,
        rationale=rationale,
    )


def _cost_fit(theme: ThemeSpec, delta_minor: int, budget: BudgetPlan) -> tuple[bool, str]:
    """Decide whether a concept can be paid for out of the existing plan."""
    if delta_minor <= 0:
        saving = abs(delta_minor)
        return True, (
            f"Saves about {budget.currency.info.symbol}{saving / 100:,.0f} against a full-price plan."
        )

    headroom = budget.contingency_minor
    symbol = budget.currency.info.symbol
    extra = f"{symbol}{delta_minor / 100:,.0f}"
    if delta_minor <= headroom:
        return True, f"Adds about {extra}, comfortably inside your contingency."
    if delta_minor <= headroom * 1.5:
        return True, f"Adds about {extra} - slightly over contingency, so trim one trade-off first."
    return (
        False,
        f"Adds about {extra}, which exceeds your contingency of {symbol}{headroom / 100:,.0f}. "
        "Adopt it only alongside a trade-off.",
    )


def catalogue_ideas(
    brief: Brief,
    budget: BudgetPlan,
    profile: EventTypeProfile,
    *,
    limit: int = DEFAULT_IDEA_COUNT,
) -> tuple[IdeaConcept, ...]:
    """Produce grounded concepts from the curated catalogue (offline mode)."""
    ranked = rank_themes(brief.taste, profile, limit=limit)
    return tuple(concept_from_theme(theme, budget, matched=matched) for theme, _, matched in ranked)


def ground_concepts(
    concepts: list[IdeaConcept],
    budget: BudgetPlan,
    taste: TasteProfile,
) -> tuple[IdeaConcept, ...]:
    """Re-ground model-drafted concepts against the engine's real numbers.

    Anything the model produced without a cost estimate is priced from the
    budget, and anything unaffordable is marked ``grounded=False`` so an
    interface can show it honestly rather than pretending it fits.
    """
    out: list[IdeaConcept] = []
    for concept in concepts:
        delta_minor = round(budget.total_minor * concept.estimated_delta_pct / 100.0)
        fits, note = _cost_fit(
            ThemeSpec(
                key=concept.key,
                name=concept.name,
                tagline=concept.tagline,
                palette=concept.palette,
                decor=concept.decor,
                menu=concept.menu,
                favors=concept.favors,
                attire=concept.attire,
                music=concept.music,
                activities=concept.activities,
                style_keywords=(),
                suitable_for=(),
                delta_pct=concept.estimated_delta_pct,
            ),
            delta_minor,
            budget,
        )
        out.append(
            IdeaConcept(
                key=concept.key,
                name=concept.name,
                tagline=concept.tagline,
                palette=concept.palette,
                decor=concept.decor,
                menu=concept.menu,
                favors=concept.favors,
                attire=concept.attire,
                music=concept.music,
                activities=concept.activities,
                estimated_delta_pct=concept.estimated_delta_pct,
                cost_fit=note,
                source=Source.MODEL,
                grounded=fits,
                rationale=concept.rationale
                or "Drafted by the model and re-priced against your budget.",
            )
        )
    return tuple(out)


def idea_warnings(ideas: tuple[IdeaConcept, ...]) -> tuple[PlanWarning, ...]:
    """Warn when a suggested concept would break the budget."""
    warnings: list[PlanWarning] = []
    for idea in ideas:
        if idea.within_budget:
            continue
        warnings.append(
            PlanWarning(
                code=f"idea_over_budget_{idea.key}",
                message=f"'{idea.name}' {idea.cost_fit}",
                severity=RiskLevel.MEDIUM,
                section="ideas",
                suggestion="See the trade-offs panel to find the money, or pick a lower-cost concept.",
            )
        )
    return tuple(warnings)
