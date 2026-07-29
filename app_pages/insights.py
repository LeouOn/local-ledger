"""Insights page: deterministic evaluation + on-demand LLM interpretation + history."""

from __future__ import annotations

import streamlit as st
from sqlmodel import select

from local_ledger.analysis.evaluate import deterministic_evaluate, llm_interpret
from local_ledger.analysis.payloads import build_evaluation_payload
from local_ledger.analysis.recommendations import RecommendationService
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import Recommendation
from local_ledger.ui.chrome import degraded_message, get_session

st.title("Insights")

session = get_session()
trends = TrendsService(session)
recs = RecommendationService(session)

month = st.text_input("Month (YYYY-MM)", value="2026-01", key="insights_month")
summary = trends.month_summary(month)

st.subheader("Deterministic findings")
evaluation = deterministic_evaluate(summary)
if evaluation.anomalies:
    for a in evaluation.anomalies:
        st.error(f"{a.kind}: {a.detail}")
else:
    st.success("No anomalies detected.")

st.divider()
st.subheader("LLM interpretation")
degraded = degraded_message("evaluate")
provider = st.radio("Provider", ["local", "cloud (opt-in)"], horizontal=True, key="insights_provider")
if st.button("Evaluate my budget"):
    payload = build_evaluation_payload(month=month, summary=summary, mom_income=[], mom_spent=[])
    if provider.startswith("cloud"):
        st.info("Cloud interpretation requires a consent token issued from the Settings page.")
    elif degraded:
        st.warning(degraded)
    else:
        try:
            from local_ledger.llm.providers import get_local_client, load_settings
            from local_ledger.llm.structured import make_openai_completer

            settings = load_settings(session)
            client = get_local_client(settings)
            completer = make_openai_completer(client)
            result = llm_interpret(payload, completer=completer, model=settings.local_model)
            st.write(result.summary)
            if result.key_findings:
                st.markdown("**Key findings**")
                for f in result.key_findings:
                    st.markdown(f"- {f}")
            if result.suggested_actions:
                st.markdown("**Suggested actions**")
                for a in result.suggested_actions:
                    st.markdown(f"- {a}")
            recs.save(
                month=month,
                provider="ollama",
                model=settings.local_model,
                prompt_hash=recs.hash_prompt(result.summary),
                body=result.summary,
            )
            st.success("Saved recommendation.")
        except Exception as exc:
            st.error(f"Interpretation failed: {exc}")

st.divider()
st.subheader("Recommendation history")
prior = list(session.exec(select(Recommendation)).all())
if prior:
    for r in sorted(prior, key=lambda x: x.created_at, reverse=True):
        col1, col2 = st.columns([4, 1])
        col1.write(f"[{r.month}] {r.body}")
        if r.followed is True:
            col2.success("followed")
        elif r.followed is False:
            col2.error("not followed")
        else:
            if col2.button("Mark followed", key=f"fol_{r.id}"):
                recs.set_followed(r.id, True, actor="user")
                st.rerun()
else:
    st.caption("No recommendations yet.")
