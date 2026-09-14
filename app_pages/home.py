"""Home / dashboard page: KPIs, budget-vs-actual, trend line, anomaly cards."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_ledger.analysis.chat import build_chat_context
from local_ledger.analysis.evaluate import deterministic_evaluate
from local_ledger.ledger.trends import TrendsService
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import kpi_card, section_header

section_header("OVERVIEW", "Local Ledger")

session = get_session()
trends = TrendsService(session)

month = st.text_input("Month (YYYY-MM)", value="2026-01", key="home_month")
summary = trends.month_summary(month)

# Refresh the persistent chat sidebar context so budget questions on this
# page reference the currently selected month's aggregates.
st.session_state["chat_context"] = build_chat_context(session, month)

col1, col2, col3, col4 = st.columns(4)
with col1:
    kpi_card("Income", f"${summary.income_cents / 100:,.2f}")
with col2:
    kpi_card("Spent", f"${summary.spent_cents / 100:,.2f}")
with col3:
    kpi_card("Budgeted", f"${summary.budgeted_cents / 100:,.2f}")
with col4:
    kpi_card(
        "Net",
        f"${(summary.income_cents - summary.spent_cents) / 100:,.2f}",
        status="ok" if summary.income_cents >= summary.spent_cents else "bad",
    )

if summary.per_category:
    df = pd.DataFrame(
        [
            {
                "category": c.category_id,
                "budgeted": c.budgeted,
                "spent": c.spent,
                "balance": c.balance,
                "carryover": c.carryover,
            }
            for c in summary.per_category
        ]
    )
    section_header("BUDGET", "Budget vs actual")
    st.bar_chart(df.set_index("category")[["budgeted", "spent"]])

    section_header("TREND", "6-month trend")
    deltas = trends.mom_deltas(month, n=6)
    trend_df = pd.DataFrame(deltas, columns=["month", "income", "spent"])
    st.line_chart(trend_df.set_index("month"))

    section_header("FINDINGS", "Anomalies")
    evaluation = deterministic_evaluate(summary)
    if evaluation.anomalies:
        for a in evaluation.anomalies:
            st.error(f"{a.kind}: {a.detail}")
    else:
        st.success("No anomalies detected this month.")
else:
    st.info("No transactions or budgets for this month yet. Import a statement on the Import page.")
