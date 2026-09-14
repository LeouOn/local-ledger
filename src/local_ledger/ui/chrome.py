"""Shared Streamlit UI helpers: DB session, local-server status banner, page chrome."""

from __future__ import annotations

import time

import streamlit as st
from sqlmodel import Session

from local_ledger.db import DEFAULT_DB_URL, ensure_data_dir, run_migrations
from local_ledger.llm import local_server, providers
from local_ledger.llm.ollama import (
    OllamaState,
    detect_state,
    guidance_for,
)

# Autostart retry cooldown: after a failed launch, do not auto-retry for this
# long (manual "Start now" is never throttled).
AUTOSTART_RETRY_COOLDOWN_S = 60.0


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


def ollama_state() -> OllamaState:
    try:
        session = get_session()
        s = providers.load_settings(session)
        root = providers._ollama_root(s.local_base_url)
        return detect_state(root, s.local_model)
    except Exception:
        return detect_state()


def _server_status_block(status: local_server.ServerStatus) -> None:
    """Render one manager status line (starting / ready / failed + logs)."""
    if status.state == "starting":
        st.info(f"Local server is starting — {status.detail}")
    elif status.state == "ready":
        st.success(status.detail)
    elif status.state == "failed":
        st.error(f"Local server startup failed — {status.detail}")
        if status.logs:
            with st.expander("Startup logs"):
                st.code("\n".join(status.logs[-30:]), language="text")


def render_ollama_banner() -> None:
    """Health banner: adopt a healthy server, autostart once, show progress."""
    try:
        settings = providers.load_settings(get_session())
    except Exception:
        settings = None
    profile = local_server.profile_from_settings(settings) if settings is not None else None

    state = ollama_state()
    status = local_server.MANAGER.status()

    if state == OllamaState.READY:
        return
    if status is not None and status.state == "starting":
        _server_status_block(status)
        return

    if (
        settings is not None
        and profile is not None
        and settings.local_autostart
        and state in (OllamaState.NOT_RUNNING, OllamaState.NOT_INSTALLED)
    ):
        cooled_down = (
            status is None
            or status.state != "failed"
            or (time.monotonic() - status.started_at) > AUTOSTART_RETRY_COOLDOWN_S
        )
        if cooled_down:
            status = local_server.MANAGER.ensure_running(
                profile,
                base_url=settings.local_base_url,
                model=settings.local_model,
            )
            _server_status_block(status)
            return

    if status is not None and status.state == "failed":
        _server_status_block(status)
    profile_key = profile.key if profile is not None else "ollama"
    st.warning(f"Local LLM unavailable — {guidance_for(state, profile_key)}")


def degraded_message(feature: str) -> str | None:
    state = ollama_state()
    if state == OllamaState.READY:
        return None
    from local_ledger.llm.ollama import DEGRADED_BEHAVIOR

    return DEGRADED_BEHAVIOR[state][feature]


def settings() -> object:  # type: ignore[no-untyped-def]
    return providers.load_settings(get_session())
