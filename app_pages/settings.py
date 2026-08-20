"""Settings page: provider configuration + consent preview."""

from __future__ import annotations

import streamlit as st

from local_ledger.analysis.payloads import build_evaluation_payload
from local_ledger.ledger.trends import TrendsService
from local_ledger.llm import providers
from local_ledger.llm.ollama import GUIDANCE
from local_ledger.llm.privacy import grant_consent, preview_payload
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

section_header("LOCAL LLM", "Local LLM (Ollama)")
state = ollama_state()
st.write(f"Status: **{state.value}** — {GUIDANCE[state]}")
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

trends = TrendsService(session)
preview_summary = trends.month_summary("2026-01")
payload = build_evaluation_payload(
    month="2026-01", summary=preview_summary, mom_income=[], mom_spent=[]
)
preview = preview_payload(payload.model_dump())

st.divider()
section_header("CONSENT", "Cloud consent preview")
st.caption(
    "Cloud calls send ONLY category names and numeric totals — never raw "
    "transactions, payees, or account numbers. Issue a one-use consent token "
    "after reviewing the payload preview below."
)
st.json(preview)
if st.button("Issue one-use consent token"):
    token = grant_consent(provider="cloud", payload_preview=preview)
    st.code(token, language="text")
    st.caption("This token authorizes a single cloud call. It is consumed on use.")
