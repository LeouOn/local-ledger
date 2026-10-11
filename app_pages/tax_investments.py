"""Investment Working Paper (TAX-13).

Read-only audit view over verified 1099-INT/DIV/B and consolidated-statement
evidence: exact working-paper totals, per-lot detail with unknown basis kept
unknown, statement sanity questions with citations, and the reviewed-vs-TaxAct
comparison for every investment figure. TaxAct computes the return; this page
audits the inputs. Recording or replacing a TaxAct entry happens on the
intake/review path (``record_reconciliation_item``), never here.

Navigation wiring (one line in ``app.py``'s page list) is deliberately left to
the coordinating owner while TAX-14/TAX-15 land their pages.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_ledger.tax.investments import (
    DISCREPANCY,
    MATCHED,
    NOT_RECORDED,
    ORPHANED,
    SOURCE_DRIFT,
    InvestmentAuditService,
)
from local_ledger.tax.models import Jurisdiction
from local_ledger.tax.service import TaxWorkspaceService
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import kpi_card, section_header, status_badge

section_header("TAX PREPARATION", "Investment Working Paper")

session = get_session()
tax_svc = TaxWorkspaceService(session)

col_year, col_jur = st.columns([1, 1])
with col_year:
    tax_year = int(st.selectbox("Tax Year", ["2025", "2024", "2026"], index=0, key="inv_year_sel"))
with col_jur:
    jurisdiction = st.selectbox(
        "Jurisdiction",
        [Jurisdiction.FEDERAL, Jurisdiction.CALIFORNIA],
        index=0,
        format_func=lambda j: "Federal (US-FED)" if j == Jurisdiction.FEDERAL else "California (US-CA)",
        key="inv_jur_sel",
    )

workspace = tax_svc.get_or_create_workspace(tax_year=tax_year, jurisdiction=jurisdiction)
audit = InvestmentAuditService(session, workspace.id)
paper = audit.working_paper()


def _dollars(cents: int | None, unknown: str = "—") -> str:
    return unknown if cents is None else f"${cents / 100:,.2f}"


st.caption(
    "The extracted text is a locating aid; figures below come from VERIFIED "
    "fields and lots. TaxAct computes the return — this page audits what was entered."
)

# ---------------------------------------------------------------------------
# Headline figures
# ---------------------------------------------------------------------------

st.subheader("Headline figures")
c1, c2, c3, c4 = st.columns(4)
with c1:
    kpi_card("Net short-term gain", _dollars(paper.totals.get("net_gain_short_term")))
with c2:
    kpi_card("Net long-term gain", _dollars(paper.totals.get("net_gain_long_term")))
with c3:
    kpi_card(
        "Unknown-basis proceeds",
        _dollars(paper.unresolved_basis_proceeds_cents),
        status="bad" if paper.unresolved_basis_lot_count else None,
    )
with c4:
    kpi_card(
        "Wash-sale disallowed",
        _dollars(paper.totals.get("wash_sale_disallowed")),
        status="warn" if paper.totals.get("wash_sale_disallowed") else None,
    )
if paper.unresolved_basis_lot_count:
    st.warning(
        f"{paper.unresolved_basis_lot_count} lot(s) have no reported basis. The basis "
        "is UNKNOWN, not zero — obtain the actual basis before entering a gain."
    )

# ---------------------------------------------------------------------------
# Statement income
# ---------------------------------------------------------------------------

st.subheader("Statement income (1099-INT / 1099-DIV)")
if paper.income_lines:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Figure": line.label,
                    "Amount": _dollars(line.amount_cents),
                    "Source": line.citation,
                }
                for line in paper.income_lines
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No verified statement income fields yet. Verify fields on the Tax Intake page.")

if paper.pending_income:
    with st.expander(
        f"{len(paper.pending_income)} proposed field(s) awaiting verification (excluded from totals)"
    ):
        st.dataframe(
            pd.DataFrame(
                [
                    {"Field": p.field_name, "Amount": _dollars(p.amount_cents), "Source": p.citation}
                    for p in paper.pending_income
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

if paper.unmapped_fields:
    st.info(
        "Verified field(s) not recognised by the working paper (shown so nothing "
        "verified is silently dropped): "
        + ", ".join(f"{p.field_name} ({_dollars(p.amount_cents)})" for p in paper.unmapped_fields)
    )

# ---------------------------------------------------------------------------
# 8949 lots
# ---------------------------------------------------------------------------

st.subheader("Form 8949 lots")
if paper.sales:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Description": s.description,
                    "Term": s.term.replace("_", " ").title(),
                    "Covered": "Yes" if s.covered else "No",
                    "Acquired": s.date_acquired or "various",
                    "Sold": s.date_sold,
                    "Proceeds": _dollars(s.proceeds_cents),
                    "Basis": _dollars(s.cost_basis_cents, unknown="UNKNOWN"),
                    "Reported gain": _dollars(s.gain_loss_cents),
                    "Wash disallowed": _dollars(s.wash_sale_disallowed_cents),
                    "Net after wash": _dollars(s.net_after_wash_cents),
                }
                for s in paper.sales
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No verified disposition lots yet.")

for note in paper.correction_notes:
    st.info(note)

# ---------------------------------------------------------------------------
# Questions for the reviewer
# ---------------------------------------------------------------------------

st.subheader("Questions for the reviewer")
if paper.questions:
    st.dataframe(
        pd.DataFrame(
            [{"Code": q.code, "Question": q.message, "Citation": q.citation} for q in paper.questions]
        ),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.success("No open audit questions from the investment evidence.")

# ---------------------------------------------------------------------------
# Prior-year carryover context
# ---------------------------------------------------------------------------

if paper.carryovers:
    st.subheader("Federal capital-loss carryover (context)")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Prior year": _dollars(c.prior_year_cents, unknown="UNKNOWN"),
                    "Used this year": _dollars(c.utilized_current_year_cents),
                    "Remaining": _dollars(c.remaining_carryover_cents, unknown="—"),
                    "Prior return": c.prior_return_reference or "not recorded",
                }
                for c in paper.carryovers
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption("Federal/California differences and other carryovers: TAX-15 view.")

# ---------------------------------------------------------------------------
# TaxAct comparison
# ---------------------------------------------------------------------------

st.subheader("Reviewed working paper vs TaxAct entries")
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
badge_cols = st.columns(len(counts) or 1)
for col, (status, count) in zip(badge_cols, sorted(counts.items()), strict=False):
    with col:
        status_badge(f"{status}: {count}", tone_by_status.get(status, "neutral"))

st.dataframe(
    pd.DataFrame(
        [
            {
                "TaxAct section": r.tax_section,
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
