"""Transactions page: filterable table, manual add/edit, transfer form, review queues."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlmodel import select

from local_ledger.ledger.service import LedgerService
from local_ledger.models import Account, ReviewState, StagedTransaction, Transaction
from local_ledger.ui.chrome import get_session

st.title("Transactions")

session = get_session()
svc = LedgerService(session)

accounts = list(session.exec(select(Account)).all())
if not accounts:
    st.info("Create an account on the Budgets or Settings page first.")
    st.stop()

account_names = {a.id: a.name for a in accounts}
month = st.text_input("Month filter (YYYY-MM)", value="", key="tx_month")
uncategorized_only = st.checkbox("Uncategorized only", value=False)

stmt = select(Transaction)
rows = list(session.exec(stmt).all())
if month:
    rows = [r for r in rows if r.date.startswith(month)]
if uncategorized_only:
    rows = [r for r in rows if r.category_id is None]

if rows:
    df = pd.DataFrame(
        [
            {
                "id": r.id,
                "date": r.date,
                "account": account_names.get(r.account_id, r.account_id),
                "amount_cents": r.amount_cents,
                "category_id": r.category_id,
                "created_by": r.created_by,
            }
            for r in rows[:1000]
        ]
    )
    st.dataframe(df, use_container_width=False)
else:
    st.info("No transactions match.")

st.divider()

with st.expander("Add transaction"):
    with st.form("add_tx", clear_on_submit=True):
        acc = st.selectbox("Account", accounts, format_func=lambda a: a.name, key="add_acc")
        date = st.text_input("Date (YYYY-MM-DD)", value="2026-01-01")
        amount = st.text_input("Amount (e.g. -12.50)", value="-1.00")
        submitted = st.form_submit_button("Add")
        if submitted:
            try:
                cents = round(float(amount) * 100)
                svc.create_transaction(
                    Transaction(account_id=acc.id, date=date, amount_cents=cents),
                    actor="user",
                )
                st.success("Added.")
            except Exception as exc:
                st.error(f"Could not add: {exc}")

with st.expander("Transfer between accounts"):
    if len(accounts) < 2:
        st.info("Need at least two accounts to transfer.")
    else:
        with st.form("transfer", clear_on_submit=True):
            from_acc = st.selectbox("From", accounts, format_func=lambda a: a.name, key="tr_from")
            to_acc = st.selectbox("To", accounts, format_func=lambda a: a.name, key="tr_to", index=1)
            tdate = st.text_input("Date (YYYY-MM-DD)", value="2026-01-01", key="tr_date")
            tamount = st.text_input("Amount (positive, e.g. 100.00)", value="100.00", key="tr_amt")
            tsub = st.form_submit_button("Transfer")
            if tsub:
                try:
                    cents = round(float(tamount) * 100)
                    svc.create_transfer(
                        from_account_id=from_acc.id,
                        to_account_id=to_acc.id,
                        amount_cents=cents,
                        date=tdate,
                        actor="user",
                    )
                    st.success("Transfer recorded.")
                except Exception as exc:
                    st.error(f"Could not transfer: {exc}")

st.divider()
st.subheader("Review queues")

st.caption("Dedup (fuzzy) candidates")
fuzzy = [s for s in session.exec(select(StagedTransaction)).all() if s.dedup_status == "FUZZY"]
if fuzzy:
    fdf = pd.DataFrame(
        [{"id": s.id, "date": s.date, "amount_cents": s.amount_cents, "payee": s.payee_raw} for s in fuzzy]
    )
    st.dataframe(fdf, use_container_width=False)
    if st.button("Accept first fuzzy candidate"):
        first = fuzzy[0]
        first.review_state = ReviewState.ACCEPTED
        session.add(first)
        session.commit()
        st.success("Accepted — return to Import to commit.")
else:
    st.caption("No fuzzy dedup candidates.")
