"""Rules page: create/edit/delete auto-categorization rules."""

from __future__ import annotations

import streamlit as st
from sqlmodel import select

from local_ledger.automation.rules import RuleService
from local_ledger.models import Category
from local_ledger.ui.chrome import get_session

st.title("Rules")

session = get_session()
svc = RuleService(session)

categories = list(session.exec(select(Category)).all())
category_by_id = {c.id: c.name for c in categories}

with st.expander("Create a rule"):
    with st.form("new_rule", clear_on_submit=True):
        name = st.text_input("Rule name", value="")
        payee = st.text_input("Payee contains", value="")
        notes = st.text_input("Notes contains", value="")
        col1, col2 = st.columns(2)
        amount_min = col1.number_input("Min amount ($)", min_value=0, value=0, step=1)
        amount_max = col2.number_input("Max amount ($)", min_value=0, value=0, step=1)
        if not categories:
            st.warning("Create a category first on the Budgets page.")
            cat_name = None
        else:
            cat_name = st.selectbox("Assign category", [c.name for c in categories])
        priority = st.number_input("Priority (higher wins)", min_value=0, value=10, step=1)
        submitted = st.form_submit_button("Create rule")
        if submitted and categories and cat_name:
            cat = next(c for c in categories if c.name == cat_name)
            try:
                svc.create_rule(
                    name=name or payee or notes or "Rule",
                    payee_contains=payee or None,
                    amount_min=int(amount_min * 100) if amount_min > 0 else None,
                    amount_max=int(amount_max * 100) if amount_max > 0 else None,
                    notes_contains=notes or None,
                    category_id=cat.id,
                    priority=int(priority),
                )
                st.success(f"Created rule: {name or payee}")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create rule: {exc}")

st.subheader("Your rules")
rules = svc.list_rules()
if rules:
    for rule in rules:
        cat_name = category_by_id.get(rule.category_id, f"category_{rule.category_id}")
        with st.container(border=True):
            parts = [f"**{rule.name}** → {cat_name} (priority {rule.priority})"]
            if rule.payee_contains:
                parts.append(f"payee contains '{rule.payee_contains}'")
            if rule.notes_contains:
                parts.append(f"notes contains '{rule.notes_contains}'")
            if rule.amount_min is not None or rule.amount_max is not None:
                lo = (rule.amount_min or 0) / 100
                hi = (rule.amount_max or 0) / 100
                parts.append(f"amount ${lo:,.0f}-${hi:,.0f}")
            st.markdown(" · ".join(parts))
            if st.button("Delete", key=f"del_rule_{rule.id}"):
                svc.delete_rule(rule.id)
                st.rerun()

    if st.button("Apply rules to uncategorized transactions"):
        count = svc.apply_rules_to_uncategorized(svc.list_rules())
        st.success(f"Applied rules to {count} transaction(s).")
else:
    st.info("No rules yet. Create one above.")
