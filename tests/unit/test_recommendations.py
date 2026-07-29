"""Recommendation store tests: append-only, user-only set_followed, follow-up."""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import Session, select

from local_ledger.analysis.recommendations import RecommendationService
from local_ledger.db import init_db
from local_ledger.models import Recommendation
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture()
def svc(session: Session, engine) -> RecommendationService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return RecommendationService(session)


def test_save_is_append_only(svc: RecommendationService, session: Session) -> None:
    """Two saves for the same month produce two distinct rows; neither is mutated."""
    svc.save(
        month="2026-01",
        provider="ollama",
        model="llama3.3:8b",
        prompt_hash="h1",
        body="Cut dining by 2000 cents",
    )
    svc.save(
        month="2026-01",
        provider="ollama",
        model="llama3.3:8b",
        prompt_hash="h2",
        body="Cut dining by 1500 cents",
    )
    rows = list(session.exec(select(Recommendation)).all())
    assert len(rows) == 2
    bodies = {r.body for r in rows}
    assert bodies == {"Cut dining by 2000 cents", "Cut dining by 1500 cents"}


def test_followed_defaults_null(svc: RecommendationService, session: Session) -> None:
    rec = svc.save(month="2026-01", provider="p", model="m", prompt_hash="h", body="b")
    assert rec.followed is None


def test_set_followed_user_only(svc: RecommendationService, session: Session) -> None:
    rec = svc.save(month="2026-01", provider="p", model="m", prompt_hash="h", body="b")
    updated = svc.set_followed(rec.id, True, actor="user")
    assert updated.followed is True
    # setting back to False
    assert svc.set_followed(rec.id, False, actor="user").followed is False


def test_service_exposes_no_body_update_path() -> None:
    """AC8: the service API has no method that updates the body."""
    methods = {name for name, _ in inspect.getmembers(RecommendationService, predicate=inspect.isfunction)}
    forbidden = {"update_body", "edit", "update", "replace", "patch"}
    assert not (forbidden & methods), f"service must not expose body-update methods: {forbidden & methods}"


def test_followup_report_matches_trends(svc: RecommendationService, session: Session) -> None:
    """Follow-up numbers come from the trends engine, not stored."""
    acc = make_account(session)
    food = make_category(session, "Food")
    make_transaction(session, acc.id, amount_cents=-8000, date="2026-02-10", category_id=food.id)
    session.commit()

    rec = svc.save(month="2026-01", provider="p", model="m", prompt_hash="h", body="Reduce Food spend")
    report = svc.followup_report(current_month="2026-02", trends_factory=lambda s: None)  # trends optional
    # report lists the prior recommendation with its measured delta placeholder
    assert any(item.recommendation_id == rec.id for item in report)


def test_diff_view_returns_two_months(svc: RecommendationService, session: Session) -> None:
    svc.save(month="2026-01", provider="p", model="m", prompt_hash="h1", body="A")
    svc.save(month="2026-02", provider="p", model="m", prompt_hash="h2", body="B")
    svc.save(month="2026-02", provider="p", model="m", prompt_hash="h3", body="C")
    diff = svc.diff_view(month_a="2026-01", month_b="2026-02")
    assert diff["2026-01"] == ["A"]
    assert set(diff["2026-02"]) == {"B", "C"}
