"""Scenario engine tests: pure-math projections, no LLM."""

from __future__ import annotations

from local_ledger.analysis.scenarios import (
    ProjectedMonth,
    ScenarioResult,
    project_forward,
    what_if_scenario,
)
from local_ledger.ledger.trends import CategoryMonth, MonthSummary


def _food_month(spent: int = 40000, budgeted: int = 30000) -> MonthSummary:
    return MonthSummary(
        month="2026-01",
        income_cents=200000,
        spent_cents=spent,
        budgeted_cents=budgeted,
        per_category=[
            CategoryMonth(
                category_id=1,
                name="Food",
                budgeted=budgeted,
                spent=spent,
                balance=budgeted - spent,
                carryover=0,
            ),
        ],
    )


def test_what_if_scenario_reduces_spent() -> None:
    base = _food_month(spent=40000)
    result = what_if_scenario(base, category_id=1, pct_change=-0.20)
    assert isinstance(result, ScenarioResult)
    assert result.original_spent == 40000
    assert result.adjusted_spent == 32000
    assert result.savings_delta_cents == 8000


def test_what_if_scenario_year_end_delta() -> None:
    base = _food_month(spent=40000)
    result = what_if_scenario(base, category_id=1, pct_change=-0.20, months_remaining=5)
    assert result.year_end_delta_cents == 8000 * 5


def test_what_if_scenario_unknown_category_raises() -> None:
    base = _food_month(spent=40000)
    import pytest

    with pytest.raises(ValueError, match="not in month summary"):
        what_if_scenario(base, category_id=999, pct_change=-0.20)


def test_project_forward_averages_and_adjusts() -> None:
    months = [
        _food_month(spent=30000),
        _food_month(spent=50000),
    ]
    projections = project_forward(months, months_ahead=2, adjustments={1: -0.10})
    assert len(projections) == 2
    # average of 30000, 50000 = 40000; adjusted by -10% = 36000
    assert projections[0].per_category_spent[1] == 36000
    assert projections[0].projected_income_cents == 200000
    assert projections[1].per_category_spent[1] == 36000


def test_project_forward_empty_summaries() -> None:
    assert project_forward([], months_ahead=3) == []


def test_scenarios_module_has_no_llm_imports() -> None:
    import inspect

    from local_ledger.analysis import scenarios

    src = inspect.getsource(scenarios)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
