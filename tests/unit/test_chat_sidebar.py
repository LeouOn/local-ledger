"""Chat sidebar tests: app wiring, module surface, LLM response path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_ledger.ui.chat_sidebar import format_evidence, generate_chat_response

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
    ).answer
    assert "food spend" in answer.lower()


def test_generate_chat_response_without_context() -> None:
    fake_response = json.dumps({"answer": "I need more data to answer that."})

    def fake_completer(messages, model):
        assert "Current month" not in messages[1]["content"]
        return fake_response

    answer = generate_chat_response(
        "How am I doing?", None, completer=fake_completer, model="m"
    ).answer
    assert "need more data" in answer.lower()


def test_generate_chat_response_sends_history_to_model() -> None:
    """Prior turns must reach the model so follow-ups keep context."""
    fake_response = json.dumps({"answer": "Yes, dining out is the rest of it."})

    captured: list[list[dict]] = []

    def fake_completer(messages, model):
        captured.append(messages)
        return fake_response

    history = [
        {"role": "user", "content": "Why is food high?"},
        {"role": "assistant", "content": "Food is $100 over budget in 2026-01."},
    ]
    result = generate_chat_response(
        "What about dining out?",
        None,
        completer=fake_completer,
        model="m",
        history=history,
    )

    messages = captured[0]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "Why is food high?"
    assert messages[2]["content"] == "Food is $100 over budget in 2026-01."
    assert messages[3]["content"] == "What about dining out?"
    assert "dining out" in result.answer.lower()


def test_generate_chat_response_parses_citations() -> None:
    fake_response = json.dumps(
        {
            "answer": "Food is $100 over budget.",
            "citations": [
                {"month": "2026-01", "category": "Food"},
                {"month": "2026-01"},
            ],
        }
    )

    result = generate_chat_response(
        "How is food?", None, completer=lambda m, mo: fake_response, model="m"
    )
    assert result.citations[0].month == "2026-01"
    assert result.citations[0].category == "Food"
    assert result.citations[1].category is None


def test_format_evidence_deduplicates_and_joins() -> None:
    from local_ledger.ui.chat_sidebar import ChatCitation

    evidence = format_evidence(
        [
            ChatCitation(month="2026-01", category="Food"),
            ChatCitation(month="2026-01", category="Food"),
            ChatCitation(month="2026-01"),
            ChatCitation(),
        ]
    )
    assert evidence == "2026-01 · Food; 2026-01"


def test_generate_chat_response_llm_error_propagates() -> None:
    def failing_completer(messages, model):
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        generate_chat_response("test", None, completer=failing_completer, model="m")
