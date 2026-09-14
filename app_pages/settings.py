"""Settings page: provider configuration + cloud consent policy."""

from __future__ import annotations

import streamlit as st

from local_ledger.llm import local_server, providers
from local_ledger.llm.ollama import guidance_for
from local_ledger.ui.chrome import get_session, ollama_state
from local_ledger.ui.components import section_header
from local_ledger.ui.theme import inject_css

st.title("Settings")

session = get_session()
settings = providers.load_settings(session)

section_header("DISPLAY", "Theme")

mode = st.radio(
    "Theme",
    ["light", "dark"],
    index=0 if settings.theme_mode == "light" else 1,
    horizontal=True,
    key="theme_mode_radio",
)
if mode != settings.theme_mode:
    providers.save_settings(session, theme_mode=mode)
    inject_css(mode)
    st.success(f"Theme switched to {mode}.")

st.divider()

section_header("LOCAL LLM", "Local model server")

profile_keys = list(local_server.PROFILES)
current_key = (
    settings.local_profile
    if settings.local_profile in local_server.PROFILES
    else local_server.DEFAULT_PROFILE_KEY
)
chosen_key = st.selectbox(
    "Server profile",
    profile_keys,
    index=profile_keys.index(current_key),
    format_func=lambda k: local_server.PROFILES[k].label,
    key="local_profile_select",
)
if chosen_key != current_key:
    # Selecting a profile applies its endpoint and model; the fields stay
    # manually overridable below.
    profile = local_server.PROFILES[chosen_key]
    providers.save_settings(
        session,
        local_profile=chosen_key,
        local_base_url=profile.base_url,
        local_model=profile.model,
    )
    st.rerun()

profile = local_server.PROFILES[chosen_key]
state = ollama_state()
st.write(f"Status: **{state.value}** — {guidance_for(state, profile.key)}")

autostart = st.checkbox(
    "Start local server when needed",
    value=settings.local_autostart,
    help="Automatically launch the selected profile when no healthy server is "
    "listening. A running server is never stopped or replaced.",
    key="local_autostart_toggle",
)
if autostart != settings.local_autostart:
    providers.save_settings(session, local_autostart=autostart)
    st.rerun()

manager_status = local_server.MANAGER.status()
if manager_status is not None:
    if manager_status.state == "starting":
        st.info(f"Starting — {manager_status.detail}")
    elif manager_status.state == "ready":
        st.success(manager_status.detail)
    elif manager_status.state == "failed":
        st.error(f"Startup failed — {manager_status.detail}")
        if manager_status.logs:
            with st.expander("Startup logs"):
                st.code("\n".join(manager_status.logs[-30:]), language="text")

if st.button("Start local server now"):
    local_server.MANAGER.ensure_running(
        profile,
        base_url=settings.local_base_url,
        model=settings.local_model,
    )
    st.rerun()

with st.form("local_settings"):
    local_url = st.text_input("Local base URL", value=settings.local_base_url)
    local_model = st.text_input("Local model", value=settings.local_model)
    if st.form_submit_button("Save local settings"):
        providers.save_settings(session, local_base_url=local_url, local_model=local_model)
        st.success("Saved.")

section_header("CLOUD", "Cloud provider (opt-in, per call)")
with st.form("cloud_settings"):
    cloud_url = st.text_input("Cloud base URL", value=settings.cloud_base_url or "")
    cloud_model = st.text_input("Cloud model", value=settings.cloud_model or "")
    cloud_key_env = st.text_input("API key ENV var name", value=settings.cloud_api_key_env or "")
    if st.form_submit_button("Save cloud settings"):
        providers.save_settings(
            session,
            cloud_base_url=cloud_url or None,
            cloud_model=cloud_model or None,
            cloud_api_key_env=cloud_key_env or None,
        )
        st.success("Saved. Keys are read from the named env var only — never stored.")

st.divider()
section_header("CONSENT", "Cloud consent")
st.caption(
    "Cloud calls send ONLY category names and numeric totals — never raw "
    "transactions, payees, or account numbers. One-use consent tokens are "
    "issued on the Insights page, right where the call happens: you preview "
    "the exact outbound payload, and the token is bound to precisely it plus "
    "the endpoint and model configured above. Any change to the data, model, "
    "or destination invalidates it."
)
