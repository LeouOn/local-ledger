"""Budgets page: per-category monthly allocations + carryover toggle."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlmodel import select

from local_ledger.ledger.service import LedgerService
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import Account, Category, CategoryBudget
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import section_header

section_header("ALLOCATION", "Budgets")

session = get_session()
svc = LedgerService(session)

accounts = list(session.exec(select(Account)).all())
if not accounts:
    name = st.text_input("Create your first account", value="Checking")
    if st.button("Create account"):
        svc.create_account(name=name, type="checking")
        st.rerun()
    st.stop()

section_header("ACCOUNTS", "Accounts")
for a in accounts:
    st.write(f"- {a.name} ({a.type})")

with st.form("new_account", clear_on_submit=True):
    aname = st.text_input("New account name", value="")
    atype = st.selectbox("Type", ["checking", "savings", "credit", "cash"])
    if st.form_submit_button("Add account"):
        svc.create_account(name=aname or atype, type=atype)
        st.rerun()

st.divider()

categories = list(session.exec(select(Category)).all())
month = st.text_input("Month (YYYY-MM)", value="2026-01", key="bud_month")

section_header("ALLOCATIONS", f"Category allocations — {month}")
existing = {
    b.category_id: b
    for b in session.exec(select(CategoryBudget).where(CategoryBudget.month == month)).all()
}
trends = TrendsService(session)
summary = trends.month_summary(month)
balance_by_cat = {c.category_id: c for c in summary.per_category}

rows = []
for cat in categories:
    b = existing.get(cat.id)
    rows.append(
        {
            "category_id": cat.id,
            "name": cat.name,
            "allocated_dollars": (b.allocated_cents / 100) if b else 0.0,
            "carryover": b.carryover_enabled if b else False,
            "spent_dollars": (balance_by_cat[cat.id].spent / 100) if cat.id in balance_by_cat else 0.0,
            "balance_dollars": (balance_by_cat[cat.id].balance / 100) if cat.id in balance_by_cat else 0.0,
        }
    )

if rows:
    edited = st.data_editor(
        pd.DataFrame(rows),
        num_rows="dynamic",
        disabled=["category_id", "spent_dollars", "balance_dollars"],
        key="budget_editor",
    )
    if st.button("Save allocations"):
        for _, r in edited.iterrows():
            allocated = round(float(r["allocated_dollars"]) * 100)
            carryover = bool(r["carryover"])
            b = existing.get(int(r["category_id"]))
            if b is None:
                session.add(
                    CategoryBudget(
                        category_id=int(r["category_id"]),
                        month=month,
                        allocated_cents=allocated,
                        carryover_enabled=carryover,
                    )
                )
            else:
                b.allocated_cents = allocated
                b.carryover_enabled = carryover
                session.add(b)
        session.commit()
        st.success("Saved.")
        st.rerun()
else:
    st.info("No categories yet. Add categories below.")

with st.form("new_category", clear_on_submit=True):
    cname = st.text_input("New category name", value="")
    cincome = st.checkbox("Income category")
    if st.form_submit_button("Add category"):
        svc.create_category(name=cname or "Uncategorized", is_income=cincome)
        st.rerun()
