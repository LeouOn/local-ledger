"""Scenario/projection engine: pure integer-cents math, no LLM imports."""

from __future__ import annotations

from dataclasses import dataclass

from local_ledger.ledger.trends import MonthSummary


@dataclass
class ScenarioResult:
    original_spent: int
    adjusted_spent: int
    savings_delta_cents: int
    year_end_delta_cents: int


@dataclass
class ProjectedMonth:
    month: str
    projected_income_cents: int
    projected_spent_cents: int
    per_category_spent: dict[int, int]


def what_if_scenario(
    base: MonthSummary,
    category_id: int,
    pct_change: float,
    months_remaining: int = 0,
) -> ScenarioResult:
    cat = next((c for c in base.per_category if c.category_id == category_id), None)
    if cat is None:
        raise ValueError(f"category_id {category_id} not in month summary")
    original = cat.spent
    adjusted = round(original * (1 + pct_change))
    delta = original - adjusted
    return ScenarioResult(
        original_spent=original,
        adjusted_spent=adjusted,
        savings_delta_cents=delta,
        year_end_delta_cents=delta * months_remaining,
    )


def project_forward(
    summaries: list[MonthSummary],
    months_ahead: int,
    adjustments: dict[int, float] | None = None,
) -> list[ProjectedMonth]:
    if not summaries:
        return []
    adjustments = adjustments or {}
    n = len(summaries)

    all_cats: set[int] = set()
    for s in summaries:
        for c in s.per_category:
            all_cats.add(c.category_id)

    avg_spent: dict[int, int] = {}
    for cat_id in all_cats:
        total = sum(
            next((c.spent for c in s.per_category if c.category_id == cat_id), 0)
            for s in summaries
        )
        avg_spent[cat_id] = round(total / n)

    avg_income = round(sum(s.income_cents for s in summaries) / n)

    projections: list[ProjectedMonth] = []
    for i in range(1, months_ahead + 1):
        per_cat: dict[int, int] = {}
        for cat_id in sorted(all_cats):
            base_amount = avg_spent[cat_id]
            pct = adjustments.get(cat_id, 0.0)
            per_cat[cat_id] = round(base_amount * (1 + pct))
        total_spent = sum(per_cat.values())
        projections.append(
            ProjectedMonth(
                month=f"+{i}",
                projected_income_cents=avg_income,
                projected_spent_cents=total_spent,
                per_category_spent=per_cat,
            )
        )
    return projections
