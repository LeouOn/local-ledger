"""ChatService tests: message persistence."""

from __future__ import annotations

import pytest

from local_ledger.analysis.chat import ChatService
from local_ledger.db import init_db


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
