"""Persistent chat sidebar renderer.

Renders a chat interface in Streamlit's sidebar that survives page navigations.
The LLM sees aggregates-only data (privacy posture unchanged from v1).
"""

from __future__ import annotations

import streamlit as st
from pydantic import BaseModel

from local_ledger.analysis.chat import ChatService
from local_ledger.llm.providers import get_local_client, load_settings
from local_ledger.llm.structured import extract, make_openai_completer
from local_ledger.ui.chrome import degraded_message, get_session

MAX_HISTORY = 20


def _build_context_text(context: dict | None) -> str:
    if not context:
        return ""
    parts: list[str] = []
    if "month" in context:
        parts.append(f"Current month: {context['month']}")
    if "summary_text" in context:
        parts.append(context["summary_text"])
    return "\n".join(parts)


def generate_chat_response(
    prompt: str,
    context: dict | None,
    *,
    completer,
    model: str,
) -> str:
    """Generate an LLM response to a budget question using aggregate context.

    Testable core of the chat sidebar: builds the prompt from context + question,
    calls the LLM, and returns the text answer. Raises on LLM failure so callers
    can degrade gracefully.
    """
    context_text = _build_context_text(context)
    user_message = f"{context_text}\n\nUser question: {prompt}" if context_text else prompt

    class _ChatResponse(BaseModel):
        answer: str

    messages = [
        {
            "role": "system",
            "content": (
                "You are a budget assistant. Answer the user's question using ONLY "
                "the provided aggregate financial data. Never invent numbers. Keep "
                "answers concise and actionable."
            ),
        },
        {"role": "user", "content": user_message},
    ]
    response = extract(completer, _ChatResponse, messages, model)
    return response.answer


def render_chat_sidebar(context: dict | None = None) -> None:
    """Render the persistent chat sidebar. Call this from app.py main()."""
    session = get_session()
    chat = ChatService(session)

    with st.sidebar:
        st.subheader("Budget chat")
        degraded = degraded_message("evaluate")
        if degraded:
            st.warning(degraded)

        history = chat.get_history(limit=MAX_HISTORY)
        for msg in history:
            with st.chat_message(msg.role):
                st.markdown(msg.content)

        if prompt := st.chat_input("Ask about your budget...", key="chat_input"):
            chat.add_message("user", prompt)
            with st.chat_message("user"):
                st.markdown(prompt)

            if degraded:
                assistant_msg = f"I can't respond right now: {degraded}"
            else:
                try:
                    settings = load_settings(session)
                    client = get_local_client(settings)
                    completer = make_openai_completer(client)
                    assistant_msg = generate_chat_response(
                        prompt, context, completer=completer, model=settings.local_model
                    )
                except Exception:
                    assistant_msg = (
                        "Sorry, I couldn't process that. Check that the local model is "
                        "running and try again."
                    )

            chat.add_message("assistant", assistant_msg)
            with st.chat_message("assistant"):
                st.markdown(assistant_msg)
