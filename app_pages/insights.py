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
from local_ledger.ui.components import section_header, status_badge

section_header("ANALYSIS", "Insights")

session = get_session()
trends = TrendsService(session)
recs = RecommendationService(session)

month = st.text_input("Month (YYYY-MM)", value="2026-01", key="insights_month")
summary = trends.month_summary(month)

# Inject context for the persistent chat sidebar so the LLM can reference
# the current month's aggregates when answering questions.
lines = [f"Month {month}: income ${summary.income_cents / 100:,.0f}, spent ${summary.spent_cents / 100:,.0f}, budgeted ${summary.budgeted_cents / 100:,.0f}."]
for c in summary.per_category:
    lines.append(f"  {c.name}: budgeted ${c.budgeted / 100:,.0f}, spent ${c.spent / 100:,.0f}, balance ${c.balance / 100:,.0f}.")
st.session_state["chat_context"] = {"month": month, "summary_text": "\n".join(lines)}

section_header("EVAL", "Deterministic findings")
evaluation = deterministic_evaluate(summary)
if evaluation.anomalies:
    for a in evaluation.anomalies:
        st.error(f"{a.kind}: {a.detail}")
else:
    st.success("No anomalies detected.")

st.divider()
section_header("INTERPRET", "LLM interpretation")
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
section_header("HISTORY", "Recommendation history")
prior = list(session.exec(select(Recommendation)).all())
if prior:
    for r in sorted(prior, key=lambda x: x.created_at, reverse=True):
        col1, col2 = st.columns([4, 1])
        col1.write(f"[{r.month}] {r.body}")
        if r.followed is True:
            with col2:
                status_badge("followed", tone="ok")
        elif r.followed is False:
            with col2:
                status_badge("not followed", tone="bad")
        else:
            if col2.button("Mark followed", key=f"fol_{r.id}"):
                recs.set_followed(r.id, True, actor="user")
                st.rerun()
    else:
        st.caption("No recommendations yet.")

st.divider()
section_header("TREND", "Multi-month narrative")
n_months = st.selectbox(
    "Months to analyze", [3, 6, 12], index=0, key="trend_n_months"
)
if st.button("Generate trend narrative"):
    from local_ledger.analysis.multimonth import build_multimonth_payload
    from local_ledger.analysis.narrative import generate_trend_narrative

    target_months: list[str] = []
    y, m = (int(x) for x in month.split("-"))
    for _ in range(n_months):
        target_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    target_months.reverse()

    summaries = [trends.month_summary(mo) for mo in target_months]
    payload = build_multimonth_payload(months=target_months, summaries=summaries)

    if degraded:
        st.warning(degraded)
    else:
        try:
            from local_ledger.llm.providers import get_local_client, load_settings
            from local_ledger.llm.structured import make_openai_completer

            settings = load_settings(session)
            client = get_local_client(settings)
            completer = make_openai_completer(client)
            narrative = generate_trend_narrative(
                payload, completer=completer, model=settings.local_model
            )
            st.markdown(f"**{narrative.summary}**")
            if narrative.key_trends:
                st.markdown("**Key trends**")
                for t in narrative.key_trends:
                    st.markdown(f"- {t}")
            if narrative.projections:
                st.markdown("**Projections**")
                for p in narrative.projections:
                    st.markdown(f"- {p}")
            if narrative.warnings:
                st.markdown("**Warnings**")
                for w in narrative.warnings:
                    st.warning(w)
            recs.save(
                month=month,
                provider="ollama",
                model=settings.local_model,
                prompt_hash=recs.hash_prompt(narrative.summary),
                body=narrative.summary,
            )
            st.success("Narrative saved to history.")
        except Exception as exc:
            st.error(f"Narrative failed: {exc}")

st.divider()
section_header("WHAT IF", "Scenarios")
if summary.per_category:
    from local_ledger.analysis.scenarios import what_if_scenario

    for cat in summary.per_category:
        col1, col2 = st.columns([2, 3])
        col1.markdown(f"**{cat.name}** (${cat.spent / 100:,.0f}/mo)")
        pct = col2.slider(
            f"Adjust {cat.name}",
            min_value=-50,
            max_value=50,
            value=0,
            step=5,
            format="%d%%",
            key=f"whatif_{cat.category_id}",
            label_visibility="collapsed",
        )
        if pct != 0:
            result = what_if_scenario(summary, cat.category_id, pct / 100, months_remaining=11)
            st.caption(
                f"{cat.name}: ${result.original_spent / 100:,.0f}/mo → "
                f"${result.adjusted_spent / 100:,.0f}/mo "
                f"(saves ${result.savings_delta_cents / 100:,.0f}/mo, "
                f"${result.year_end_delta_cents / 100:,.0f} by year end)"
            )
else:
    st.caption("No budget data for this month.")
