"""Shared Streamlit UI helpers: DB session, Ollama status banner, page chrome."""

from __future__ import annotations

import streamlit as st
from sqlmodel import Session

from local_ledger.db import DEFAULT_DB_URL, ensure_data_dir, run_migrations
from local_ledger.llm import providers
from local_ledger.llm.ollama import (
    DEGRADED_BEHAVIOR,
    GUIDANCE,
    OllamaState,
    detect_state,
)


@st.cache_resource
def get_session() -> Session:  # type: ignore[no-untyped-def]
    """One cached session bound to the app's file DB; migrations run once.

    The DB URL is overridable via ``LOCAL_LEDGER_DB_URL`` so AppTest can target a
    throwaway file database.
    """
    import os

    from sqlalchemy import create_engine

    db_url = os.environ.get("LOCAL_LEDGER_DB_URL", DEFAULT_DB_URL)
    ensure_data_dir()
    run_migrations(db_url)
    engine = create_engine(db_url)
    return Session(engine)


@st.cache_resource
def _state_detector():  # type: ignore[no-untyped-def]
    return detect_state()


def ollama_state() -> OllamaState:
    return _state_detector()


def render_ollama_banner() -> None:
    state = ollama_state()
    if state == OllamaState.READY:
        return
    st.warning(f"Local LLM unavailable — {GUIDANCE[state]}")


def degraded_message(feature: str) -> str | None:
    state = ollama_state()
    if state == OllamaState.READY:
        return None
    return DEGRADED_BEHAVIOR[state][feature]


def settings() -> object:  # type: ignore[no-untyped-def]
    return providers.load_settings(get_session())
