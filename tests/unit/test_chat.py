"""ChatService tests: message persistence + shared chat context."""

from __future__ import annotations

import pytest

from local_ledger.analysis.chat import ChatService, build_chat_context, latest_data_month
from local_ledger.db import init_db
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


def test_add_and_get_history(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "Hello")
    svc.add_message("assistant", "Hi there")
    history = svc.get_history()
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"


def test_history_ordered_oldest_first(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "First")
    svc.add_message("user", "Second")
    history = svc.get_history()
    assert history[0].content == "First"
    assert history[1].content == "Second"


def test_history_limit(session) -> None:
    svc = ChatService(session)
    for i in range(60):
        svc.add_message("user", f"msg-{i}")
    assert len(svc.get_history()) == 50
    assert svc.get_history()[-1].content == "msg-59"


def test_clear_history(session) -> None:
    svc = ChatService(session)
    svc.add_message("user", "Hello")
    svc.clear_history()
    assert svc.get_history() == []


# --- shared chat context ---


def test_build_chat_context_reports_month_and_categories(session) -> None:
    account = make_account(session)
    food = make_category(session, name="Food")
    make_transaction(session, account.id, amount_cents=-40000, category_id=food.id)
    make_transaction(session, account.id, amount_cents=300000, date="2026-01-02")

    context = build_chat_context(session, "2026-01")
    assert context["month"] == "2026-01"
    assert "income $3,000" in context["summary_text"]
    assert "spent $400" in context["summary_text"]
    assert "Food" in context["summary_text"]


def test_build_context_empty_month_has_no_categories(session) -> None:
    context = build_chat_context(session, "2030-01")
    assert context["month"] == "2030-01"
    assert "income $0" in context["summary_text"]
    assert "Food" not in context["summary_text"]


def test_latest_data_month(session) -> None:
    assert latest_data_month(session) is None
    account = make_account(session)
    make_transaction(session, account.id, date="2026-01-15")
    make_transaction(session, account.id, date="2026-03-10")
    assert latest_data_month(session) == "2026-03"
