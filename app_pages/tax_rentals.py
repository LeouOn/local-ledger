"""Rental Working Papers (TAX-14).

Read-only audit view per property: verified rent/expense evidence, separated
not-an-expense lines (mortgage principal, deposits), escrow/1098 cross-check
questions with citations, depreciation and suspended-loss history context,
and the reviewed-vs-TaxAct comparison for every Schedule E figure. TaxAct
computes the return; the reviewer decides treatment; this page audits inputs.

Navigation wiring (one line in ``app.py``'s page list) is left to the
coordinating owner while the audit-wave pages merge.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_ledger.tax.models import Jurisdiction
from local_ledger.tax.rentals import (
    DISCREPANCY,
    MATCHED,
    NOT_RECORDED,
    ORPHANED,
    SOURCE_DRIFT,
    RentalAuditService,
)
from local_ledger.tax.service import TaxWorkspaceService
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import kpi_card, section_header, status_badge

section_header("TAX PREPARATION", "Rental Working Papers")

session = get_session()
tax_svc = TaxWorkspaceService(session)

col_year, col_jur = st.columns([1, 1])
with col_year:
    tax_year = int(st.selectbox("Tax Year", ["2025", "2024", "2026"], index=0, key="rent_year_sel"))
with col_jur:
    jurisdiction = st.selectbox(
        "Jurisdiction",
        [Jurisdiction.FEDERAL, Jurisdiction.CALIFORNIA],
        index=0,
        format_func=lambda j: "Federal (US-FED)" if j == Jurisdiction.FEDERAL else "California (US-CA)",
        key="rent_jur_sel",
    )

workspace = tax_svc.get_or_create_workspace(tax_year=tax_year, jurisdiction=jurisdiction)
audit = RentalAuditService(session, workspace.id)
papers = audit.working_papers()


def _dollars(cents: int | None, unknown: str = "—") -> str:
    return unknown if cents is None else f"${cents / 100:,.2f}"


st.caption(
    "Figures come from VERIFIED expense records and statement fields. Mortgage "
    "principal and deposits are separated, never summed as expenses. TaxAct "
    "computes the return — this page audits what was entered."
)

if not papers:
    st.info("No rental properties recorded in this workspace yet.")
    st.stop()

for paper in papers:
    checks = "✅ fully checked" if paper.fully_checked else "⚠️ unresolved questions"
    with st.container(border=True):
        st.markdown(f"### {paper.label}")
        st.caption(
            f"{paper.property_type} · placed in service "
            f"{paper.date_placed_in_service or 'UNKNOWN'} · ownership "
            f"{paper.ownership_basis_points / 100:.2f}% · {paper.days_rented} rented / "
            f"{paper.days_personal_use} personal-use day(s) · {checks}"
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            kpi_card("Gross rents", _dollars(paper.totals.get("rents")))
        with c2:
            kpi_card("Operating expenses", _dollars(paper.totals.get("total_operating_expenses")))
        with c3:
            kpi_card(
                "Separated (not expenses)",
                _dollars(paper.totals.get("separated_not_expenses")),
                status="warn" if paper.totals.get("separated_not_expenses") else None,
            )

        expense_rows = [
            {
                "Category": line.category,
                "Amount": _dollars(line.amount_cents),
                "Separated": "not an expense" if line.separated else "",
                "Source": line.citation,
            }
            for line in paper.expense_lines
        ]
        if expense_rows:
            st.dataframe(pd.DataFrame(expense_rows), use_container_width=True, hide_index=True)
        else:
            st.info("No verified expense records yet.")

        if paper.income_lines:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Figure": line.label, "Amount": _dollars(line.amount_cents), "Source": line.citation}
                        for line in paper.income_lines
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

        for note in paper.correction_notes:
            st.info(note)

        if paper.questions:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Code": q.code, "Question": q.message, "Citation": q.citation}
                        for q in paper.questions
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

        if paper.depreciation:
            st.caption("Depreciation history (never inferred):")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Asset": d.description,
                            "Placed in service": d.date_placed_in_service,
                            "Depreciable basis": _dollars(d.depreciable_basis_cents),
                            "Recovery (months)": d.recovery_period_months,
                            "Prior federal": _dollars(d.prior_fed_cents),
                            "Prior California": _dollars(d.prior_ca_cents),
                            "Current federal": _dollars(d.current_fed_cents),
                            "Current California": _dollars(d.current_ca_cents),
                        }
                        for d in paper.depreciation
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

        if paper.suspended_losses:
            st.caption("Suspended passive-loss carryovers (UNKNOWN means missing history, not zero):")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Category": c.category,
                            "Prior year": _dollars(c.prior_year_cents, unknown="UNKNOWN"),
                            "Remaining": _dollars(c.remaining_carryover_cents),
                            "Prior return": c.prior_return_reference or "not recorded",
                        }
                        for c in paper.suspended_losses
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

# ---------------------------------------------------------------------------
# TaxAct comparison across all properties
# ---------------------------------------------------------------------------

st.subheader("Reviewed working papers vs TaxAct entries (Schedule E)")
rows = audit.comparisons()
tone_by_status = {
    MATCHED: "ok",
    DISCREPANCY: "bad",
    SOURCE_DRIFT: "warn",
    NOT_RECORDED: "neutral",
    ORPHANED: "warn",
}
counts: dict[str, int] = {}
for row in rows:
    counts[row.status] = counts.get(row.status, 0) + 1
if counts:
    badge_cols = st.columns(len(counts))
    for col, (status, count) in zip(badge_cols, sorted(counts.items()), strict=False):
        with col:
            status_badge(f"{status}: {count}", tone_by_status.get(status, "neutral"))

if rows:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Figure": r.field_key,
                    "Working paper": _dollars(r.working_paper_cents),
                    "TaxAct entered": _dollars(r.taxact_entered_cents),
                    "Delta": _dollars(r.delta_cents),
                    "Status": r.status,
                    "Note": r.note,
                }
                for r in rows
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "MATCHED: TaxAct equals the reviewed figure. DISCREPANCY: exact delta shown. "
        "SOURCE_DRIFT: the paper moved since the entry was recorded. NOT_RECORDED: no "
        "reviewed entry yet. ORPHANED: an entry exists for a figure the paper no longer produces."
    )
