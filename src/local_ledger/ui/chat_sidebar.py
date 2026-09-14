"""Persistent chat sidebar renderer.

Renders a chat interface in Streamlit's sidebar that survives page navigations.
The LLM sees aggregates-only data (privacy posture unchanged from v1), prior
conversation turns for follow-up memory, and must return month/category
citations so numerical answers show their evidence.
"""

from __future__ import annotations

import streamlit as st
from pydantic import BaseModel

from local_ledger.analysis.chat import ChatService, refresh_chat_context
from local_ledger.llm.providers import get_local_client, load_settings
from local_ledger.llm.structured import extract, make_openai_completer
from local_ledger.ui.chrome import degraded_message, get_session

MAX_HISTORY = 20

SYSTEM_PROMPT = (
    "You are a budget assistant. Answer the user's question using ONLY the "
    "provided aggregate financial data and the prior conversation. Never "
    "invent numbers. Keep answers concise and actionable. Back every "
    "numerical claim with a citation naming the month and category it comes "
    "from (month alone for month-level totals); return no citation for "
    "non-numeric claims."
)


class ChatCitation(BaseModel):
    month: str | None = None
    category: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[ChatCitation] = []


def _build_context_text(context: dict | None) -> str:
    if not context:
        return ""
    parts: list[str] = []
    if "month" in context:
        parts.append(f"Current month: {context['month']}")
    if "summary_text" in context:
        parts.append(context["summary_text"])
    return "\n".join(parts)


def format_evidence(citations: list[ChatCitation]) -> str:
    """Render citations as a compact, de-duplicated evidence line."""
    labels: list[str] = []
    for c in citations:
        label = " · ".join(part for part in (c.month, c.category) if part)
        if label and label not in labels:
            labels.append(label)
    return "; ".join(labels)


def generate_chat_response(
    prompt: str,
    context: dict | None,
    *,
    completer,
    model: str,
    history: list[dict] | None = None,
) -> ChatResponse:
    """Generate an LLM response to a budget question using aggregate context.

    Testable core of the chat sidebar: builds the prompt from context, prior
    conversation turns, and the question, calls the LLM, and returns the text
    answer plus month/category citations for numerical claims. Raises on LLM
    failure so callers can degrade gracefully.
    """
    context_text = _build_context_text(context)
    user_message = f"{context_text}\n\nUser question: {prompt}" if context_text else prompt

    class _ChatResponse(BaseModel):
        answer: str
        citations: list[ChatCitation] = []

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in history or []:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})
    response = extract(completer, _ChatResponse, messages, model)
    return ChatResponse(answer=response.answer, citations=response.citations)


def _resolve_context(context: dict | None) -> dict | None:
    """Keep the selected month, but always rebuild its aggregates from the DB.

    Pages only communicate which month the user is looking at; the totals are
    recomputed here right before answering so imports/edits made since the
    page rendered can never serve stale numbers to the model.
    """
    if context is None:
        context = st.session_state.get("chat_context")
    refreshed = refresh_chat_context(get_session(), context)
    if refreshed is not None:
        st.session_state["chat_context"] = refreshed
    return refreshed


def render_chat_sidebar(context: dict | None = None) -> None:
    """Render the persistent chat sidebar. Call this from app.py main()."""
    session = get_session()
    chat = ChatService(session)
    context = _resolve_context(context)

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

            evidence = ""
            if degraded:
                assistant_msg = f"I can't respond right now: {degraded}"
            else:
                try:
                    settings = load_settings(session)
                    client = get_local_client(settings)
                    completer = make_openai_completer(client)
                    result = generate_chat_response(
                        prompt,
                        context,
                        completer=completer,
                        model=settings.local_model,
                        history=[{"role": m.role, "content": m.content} for m in history],
                    )
                    assistant_msg = result.answer
                    evidence = format_evidence(result.citations)
                except Exception:
                    assistant_msg = (
                        "Sorry, I couldn't process that. Check that the local model is "
                        "running and try again."
                    )

            stored = assistant_msg + (f"\n\n_Evidence: {evidence}_" if evidence else "")
            chat.add_message("assistant", stored)
            with st.chat_message("assistant"):
                st.markdown(assistant_msg)
                if evidence:
                    st.caption(f"Evidence: {evidence}")
