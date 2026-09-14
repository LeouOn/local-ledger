"""Evaluate service tests: deterministic anomalies run with LLM patched to raise."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

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


def test_drift_detection_flags_mom_change(session: Session) -> None:
    from local_ledger.ledger.trends import CategoryMonth, MonthSummary, TrendsService

    _seed(session)
    trends = TrendsService(session)
    current = trends.month_summary("2026-01")
    prior = MonthSummary(
        month="2025-12",
        income_cents=0,
        spent_cents=0,
        budgeted_cents=0,
        per_category=[
            CategoryMonth(category_id=c.category_id, name=c.name, budgeted=0, spent=1000, balance=0, carryover=0)
            for c in current.per_category
        ],
    )
    result = deterministic_evaluate(current, prior_summary=prior)
    drifts = [a for a in result.anomalies if a.kind == "drift"]
    assert len(drifts) > 0


def test_outlier_detection_flags_3sigma_transaction(session: Session) -> None:
    from local_ledger.ledger.trends import TrendsService
    from local_ledger.models import Transaction

    acc, food = _seed(session)
    for i in range(20):
        make_transaction(
            session, acc.id, amount_cents=-(1000 + i * 10),
            date=f"2026-01-{(i % 27) + 1:02d}", category_id=food.id,
        )
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-28", category_id=food.id)
    session.commit()

    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    txns = list(session.exec(select(Transaction).where(Transaction.date.startswith("2026-01"))).all())
    result = deterministic_evaluate(summary, transactions=txns)
    outliers = [a for a in result.anomalies if a.kind == "outlier"]
    assert len(outliers) > 0


def _cloud_ready(session: Session, monkeypatch: pytest.MonkeyPatch, model: str = "gpt-4o-mini"):
    """Configure cloud settings + env key; return (settings, request builder)."""
    from local_ledger.llm import providers

    settings = providers.save_settings(
        session,
        cloud_base_url="https://api.openai.com/v1",
        cloud_model=model,
        cloud_api_key_env="LOCAL_LEDGER_TEST_KEY",
    )
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    return settings


def _seeded_payload(session: Session):  # type: ignore[no-untyped-def]
    _seed(session)
    summary = TrendsService(session).month_summary("2026-01")
    return build_evaluation_payload(month="2026-01", summary=summary, mom_income=[], mom_spent=[])


def test_llm_interpret_cloud_consents_sends_and_audits_one_request(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud path: the full request object (model, messages incl. system
    prompt, response_format) is consent-verified, sent verbatim, and audited
    per transmission attempt."""
    import json as _json

    from local_ledger.analysis.evaluate import build_interpret_request
    from local_ledger.llm import cloud as cloud_module
    from local_ledger.llm import providers
    from local_ledger.llm.privacy import grant_consent
    from local_ledger.models import AuditLog

    payload = _seeded_payload(session)
    settings = _cloud_ready(session, monkeypatch)
    request = build_interpret_request(payload, model="gpt-4o-mini")
    token = grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=request
    )

    sent: list[dict] = []

    def fake_send(client, req):  # type: ignore[no-untyped-def]
        sent.append(req)
        return _json.dumps({"summary": "ok", "key_findings": [], "suggested_actions": []})

    monkeypatch.setattr(cloud_module, "send_chat_request", fake_send)

    result = llm_interpret(
        payload,
        provider="cloud",
        consent_token=token,
        settings=settings,
        session=session,
    )

    assert result.summary == "ok"
    assert sent == [request]  # exactly the consented body, nothing else
    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 1
    assert rows[0].actor == "llm:cloud:https://api.openai.com/v1:gpt-4o-mini"
    recorded = _json.loads(rows[0].after_json or "{}")
    assert recorded["attempt"] == 1
    assert recorded["outcome"] == "ok"
    assert recorded["request"] == request


def test_llm_interpret_cloud_rejects_unconsented_model_override(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model override must be consent-bound, not verified against the
    configured model and then silently sent."""
    from local_ledger.analysis.evaluate import build_interpret_request
    from local_ledger.llm import providers
    from local_ledger.llm.privacy import grant_consent
    from local_ledger.models import AuditLog

    payload = _seeded_payload(session)
    settings = _cloud_ready(session, monkeypatch)
    request = build_interpret_request(payload, model="gpt-4o-mini")
    token = grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=request
    )

    with pytest.raises(providers.CloudCallBlocked):
        llm_interpret(
            payload,
            model="unapproved-model",
            provider="cloud",
            consent_token=token,
            settings=settings,
            session=session,
        )
    # nothing was transmitted, so nothing was audited
    assert list(session.exec(select(AuditLog)).all()) == []


def test_llm_interpret_cloud_audits_every_retry_attempt(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid model output triggers retries; each transmission attempt
    writes its own audit row."""
    import json as _json

    from local_ledger.analysis.evaluate import build_interpret_request
    from local_ledger.llm import cloud as cloud_module
    from local_ledger.llm import providers
    from local_ledger.llm.privacy import grant_consent
    from local_ledger.models import AuditLog

    payload = _seeded_payload(session)
    settings = _cloud_ready(session, monkeypatch)
    request = build_interpret_request(payload, model="gpt-4o-mini")
    token = grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=request
    )

    replies = iter(
        [
            "not json at all",
            '{"summary": null, "bogus": 1}',  # valid JSON, wrong shape
            _json.dumps({"summary": "ok", "key_findings": [], "suggested_actions": []}),
        ]
    )
    monkeypatch.setattr(
        cloud_module, "send_chat_request", lambda client, req: next(replies)
    )

    result = llm_interpret(
        payload,
        provider="cloud",
        consent_token=token,
        settings=settings,
        session=session,
        max_retries=2,
    )
    assert result.summary == "ok"

    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 3  # one row per transmission, retries included
    outcomes = [_json.loads(r.after_json or "{}") for r in rows]
    assert [o["attempt"] for o in outcomes] == [1, 2, 3]
    assert all(o["outcome"] == "ok" for o in outcomes)  # transmissions succeeded


def test_llm_interpret_cloud_audits_failed_transmission(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout/error after the request left the machine is audited."""
    import json as _json

    from local_ledger.analysis.evaluate import build_interpret_request
    from local_ledger.llm import cloud as cloud_module
    from local_ledger.llm import providers
    from local_ledger.llm.privacy import grant_consent
    from local_ledger.models import AuditLog

    payload = _seeded_payload(session)
    settings = _cloud_ready(session, monkeypatch)
    request = build_interpret_request(payload, model="gpt-4o-mini")
    token = grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=request
    )

    def boom(client, req):  # type: ignore[no-untyped-def]
        raise TimeoutError("request timed out")

    monkeypatch.setattr(cloud_module, "send_chat_request", boom)

    with pytest.raises(TimeoutError):
        llm_interpret(
            payload,
            provider="cloud",
            consent_token=token,
            settings=settings,
            session=session,
        )

    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 1
    recorded = _json.loads(rows[0].after_json or "{}")
    assert recorded["outcome"] == "error"
    assert "timed out" in (recorded["error"] or "")


def test_llm_interpret_requires_completer_for_local() -> None:
    from local_ledger.analysis.payloads import build_evaluation_payload as _bep
    from local_ledger.ledger.trends import MonthSummary

    payload = _bep(month="2026-01", summary=MonthSummary(month="2026-01"), mom_income=[], mom_spent=[])
    with pytest.raises(ValueError, match="completer"):
        llm_interpret(payload, completer=None, provider="local")
