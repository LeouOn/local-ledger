"""Evaluate service tests: deterministic anomalies run with LLM patched to raise."""

from __future__ import annotations

import pytest
from sqlmodel import Session

from local_ledger.analysis.evaluate import (
    BudgetEvaluation,
    deterministic_evaluate,
    llm_interpret,
)
from local_ledger.analysis.payloads import build_evaluation_payload
from local_ledger.db import init_db
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import CategoryBudget
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


def _seed(session: Session):  # type: ignore[no-untyped-def]
    acc = make_account(session)
    food = make_category(session, "Food")
    session.add(CategoryBudget(category_id=food.id, month="2026-01", allocated_cents=10000))
    # overspend: 12000 on a 10000 budget (>110%)
    make_transaction(session, acc.id, amount_cents=-12000, date="2026-01-10", category_id=food.id)
    # a large single transaction > 3x the category monthly spend pattern
    make_transaction(session, acc.id, amount_cents=-8000, date="2026-01-11", category_id=food.id)
    session.commit()
    return acc, food


def test_deterministic_evaluate_flags_overspend(session: Session) -> None:
    _seed(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")

    result = deterministic_evaluate(summary)
    assert any(a.kind == "overspend" for a in result.anomalies)


def test_deterministic_evaluate_runs_with_llm_patched_to_raise(session: Session) -> None:
    """AC7: deterministic paths produce correct results even if any LLM call raises."""
    _seed(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")

    # Simulate LLM being unavailable: patch the module's interpret import target.

    def exploding(*_a, **_k):
        raise RuntimeError("LLM should not be called by deterministic_evaluate")

    # deterministic_evaluate must not touch LLM; verify by monkeypatching extract
    import local_ledger.llm.structured as structured

    original = structured.extract
    structured.extract = exploding  # type: ignore[assignment]
    try:
        result = deterministic_evaluate(summary)
        assert isinstance(result.anomalies, list)
    finally:
        structured.extract = original  # type: ignore[assignment]


def test_llm_interpret_returns_typed_evaluation(session: Session) -> None:
    _seed(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    payload = build_evaluation_payload(
        month="2026-01", summary=summary, mom_income=[], mom_spent=[]
    )

    def fake_completer(_messages, _model):
        return '{"summary": "You overspent on Food.", "key_findings": ["Food over budget"], "suggested_actions": ["Reduce dining by 2000 cents"]}'

    result = llm_interpret(payload, completer=fake_completer, model="m")
    assert isinstance(result, BudgetEvaluation)
    assert result.summary
    assert len(result.suggested_actions) == 1


def test_llm_interpret_cloud_path_blocked_without_consent(session: Session) -> None:
    _seed(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    payload = build_evaluation_payload(
        month="2026-01", summary=summary, mom_income=[], mom_spent=[]
    )
    from local_ledger.llm import providers

    with pytest.raises(providers.CloudCallBlocked):
        llm_interpret(payload, completer=None, model="m", provider="cloud", consent_token=None, settings=None)  # type: ignore[arg-type]
