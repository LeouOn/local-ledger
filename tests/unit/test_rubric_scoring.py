"""Unit tests for rubric scoring logic in eval quality spike."""

from __future__ import annotations

from types import SimpleNamespace

from tests.integration.test_eval_quality import score_rubric


def test_score_rubric_high_quality() -> None:
    payload = {
        "total_income_cents": 300000,
        "total_spent_cents": 320000,
        "categories": [{"name": "Food"}, {"name": "Rent"}],
    }
    evaluation = SimpleNamespace(
        summary="You overspent on Food with a deficit of $200 (62000 cents vs 40000 budgeted).",
        key_findings=["Food is 55% over budget."],
        suggested_actions=[
            "Reduce Food spending by $200 next month.",
            "Review dining out expenses weekly.",
        ],
    )
    score = score_rubric(payload, evaluation)
    assert score["status"] == "scored"
    assert score["specificity"] == 3
    assert score["correctness"] == 3
    assert score["actionability"] == 3
    assert score["total"] == 9
    assert score["passed"] is True


def test_score_rubric_low_quality() -> None:
    payload = {
        "total_income_cents": 300000,
        "total_spent_cents": 320000,
        "categories": [{"name": "Food"}, {"name": "Rent"}],
    }
    evaluation = SimpleNamespace(
        summary="You had a great surplus this month.",  # contradiction
        key_findings=[],
        suggested_actions=[],
    )
    score = score_rubric(payload, evaluation)
    assert score["status"] == "scored"
    assert score["specificity"] == 0
    assert score["correctness"] == 0
    assert score["actionability"] == 0
    assert score["total"] == 0
    assert score["passed"] is False
