"""Chat sidebar tests: app wiring, module surface, LLM response path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_ledger.ui.chat_sidebar import generate_chat_response

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_chat_sidebar_renders_in_app(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app.py")


def test_app_py_wires_chat_sidebar() -> None:
    """app.py must import and call render_chat_sidebar."""
    app_src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "from local_ledger.ui.chat_sidebar import" in app_src
    assert "render_chat_sidebar(" in app_src


def test_render_chat_sidebar_module_exists() -> None:
    """The chat_sidebar module must export render_chat_sidebar."""
    from local_ledger.ui import chat_sidebar

    assert hasattr(chat_sidebar, "render_chat_sidebar")
    assert callable(chat_sidebar.render_chat_sidebar)


def test_generate_chat_response_uses_context() -> None:
    fake_response = json.dumps({"answer": "Your food spend is $400, which is 20% over budget."})

    def fake_completer(messages, model):
        user_msg = messages[1]["content"]
        assert "Current month: 2026-01" in user_msg
        assert "Food" in user_msg
        assert "User question: Why is food high?" in user_msg
        return fake_response

    context = {
        "month": "2026-01",
        "summary_text": "Food: budgeted $300, spent $400, balance -$100.",
    }
    answer = generate_chat_response(
        "Why is food high?", context, completer=fake_completer, model="m"
    )
    assert "food spend" in answer.lower()


def test_generate_chat_response_without_context() -> None:
    fake_response = json.dumps({"answer": "I need more data to answer that."})

    def fake_completer(messages, model):
        assert "Current month" not in messages[1]["content"]
        return fake_response

    answer = generate_chat_response(
        "How am I doing?", None, completer=fake_completer, model="m"
    )
    assert "need more data" in answer.lower()


def test_generate_chat_response_llm_error_propagates() -> None:
    def failing_completer(messages, model):
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        generate_chat_response("test", None, completer=failing_completer, model="m")
