"""Savings goals page: create goals, track progress, project achievement."""

from __future__ import annotations

import streamlit as st

from local_ledger.analysis.goals import GoalService
from local_ledger.ui.chrome import get_session

st.title("Savings goals")

session = get_session()
service = GoalService(session)

with st.expander("Create a new goal"):
    with st.form("new_goal", clear_on_submit=True):
        name = st.text_input("Goal name", value="Emergency Fund")
        col1, col2 = st.columns(2)
        target = col1.number_input("Target amount ($)", min_value=100, value=10000, step=500)
        target_date = col2.text_input("Target date (YYYY-MM-DD)", value="2026-12-31")
        monthly = st.number_input("Planned monthly savings ($)", min_value=0, value=500, step=50)
        notes = st.text_area("Notes (optional)", value="")
        submitted = st.form_submit_button("Create goal")
        if submitted:
            try:
                service.create_goal(name, int(target * 100), target_date, int(monthly * 100), notes or None)
                st.success(f"Created goal: {name}")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not create goal: {exc}")

st.subheader("Your goals")
goals = service.list_goals()
if goals:
    for goal in goals:
        progress = service.current_progress(goal)
        achievement = service.projected_achievement(goal)
        on_track = service.on_track(goal)

        status = "On track" if on_track else "Behind schedule"
        badge = "green" if on_track else "red"

        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            col1.markdown(f"**{goal.name}** — ${goal.target_cents / 100:,.0f} by {goal.target_date}")
            col2.markdown(f":{badge}[{status}]")
            st.progress(min(progress.pct / 100, 1.0))
            st.caption(
                f"${progress.current_cents / 100:,.0f} saved ({progress.pct:.0f}%) · "
                f"Projected: {achievement or 'never'}"
            )
            if st.button("Delete", key=f"del_goal_{goal.id}"):
                service.delete_goal(goal.id)
                st.rerun()
else:
    st.info("No savings goals yet. Create one above.")
