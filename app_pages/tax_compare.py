"""TaxAct Comparison & Export (TAX-08).

The final screen of the manual preparation journey: every required TaxAct
field with its reviewed working-paper amount, the entered TaxAct figure, the
delta and the coverage verdict — plus the entry form that records a reviewed
comparison (retaining history via the workspace service) and the draft/final
export buttons. TaxAct computes the return; this page proves what was entered
matches what was reviewed.

Navigation wiring (one line in ``app.py``) is left to the coordinating owner.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_ledger.tax import taxact_fields
from local_ledger.tax.export import (
    ExportReadinessError,
    TaxExportError,
    default_export_root,
    export_packet,
)
from local_ledger.tax.models import Jurisdiction
from local_ledger.tax.readiness import evaluate_readiness
from local_ledger.tax.service import TaxWorkspaceService
from local_ledger.tax.storage import sanitize_path
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import section_header, status_badge

section_header("TAX PREPARATION", "TaxAct Comparison & Export")

session = get_session()
tax_svc = TaxWorkspaceService(session)

col_year, col_jur = st.columns([1, 1])
with col_year:
    tax_year = int(st.selectbox("Tax Year", ["2025", "2024", "2026"], index=0, key="cmp_year_sel"))
with col_jur:
    jurisdiction = st.selectbox(
        "Jurisdiction",
        [Jurisdiction.FEDERAL, Jurisdiction.CALIFORNIA],
        index=0,
        format_func=lambda j: "Federal (US-FED)" if j == Jurisdiction.FEDERAL else "California (US-CA)",
        key="cmp_jur_sel",
    )

workspace = tax_svc.get_or_create_workspace(tax_year=tax_year, jurisdiction=jurisdiction)
ws_id = workspace.id

readiness = evaluate_readiness(session, ws_id)
coverage = taxact_fields.coverage_report(session, ws_id)

st.caption(
    "Each row: reviewed working-paper amount vs the TaxAct entry recorded for "
    "it. Recording an entry is a review action with retained history; exports "
    "pin the exact reviewed snapshot and go stale when evidence changes."
)

# ---------------------------------------------------------------------------
# readiness + coverage summary
# ---------------------------------------------------------------------------

c1, c2, c3 = st.columns(3)
with c1:
    status_badge("READY" if readiness.ready else "NOT READY", "ok" if readiness.ready else "bad")
with c2:
    counts = coverage.counts()
    status_badge(f"covered: {counts.get(taxact_fields.COVERED, 0)}", "ok")
with c3:
    gaps = coverage.gaps
    status_badge(f"gaps: {len(gaps)}", "bad" if gaps else "neutral")

if not readiness.ready:
    with st.expander(f"Readiness reasons ({len(readiness.reasons)})"):
        for reason in readiness.reasons:
            st.markdown(f"- {reason}")
if not coverage.california_evaluated:
    st.info(coverage.california_note)

# ---------------------------------------------------------------------------
# coverage table
# ---------------------------------------------------------------------------

st.subheader("TaxAct field coverage")
if coverage.rows:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "TaxAct section": r.tax_section,
                    "Field": r.field_key,
                    "Working paper": f"${(r.working_paper_cents or 0) / 100:,.2f}"
                    if r.working_paper_cents is not None
                    else "—",
                    "TaxAct entered": f"${(r.taxact_entered_cents or 0) / 100:,.2f}"
                    if r.taxact_entered_cents is not None
                    else "not entered",
                    "Delta": f"${(r.delta_cents or 0) / 100:,.2f}" if r.delta_cents is not None else "—",
                    "Verdict": r.verdict,
                    "Note": r.note,
                }
                for r in coverage.rows
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No working-paper figures yet: verify evidence on the intake and audit pages first.")

# ---------------------------------------------------------------------------
# record a reviewed TaxAct entry
# ---------------------------------------------------------------------------

st.subheader("Record a reviewed TaxAct entry")
required_rows = [
    r for r in coverage.rows if r.required and r.verdict != taxact_fields.ORPHANED
]
if required_rows:
    options = [f"{r.tax_section} / {r.field_key}" for r in required_rows]
    with st.form("record_entry", border=True):
        choice = st.selectbox("Field", options, key="cmp_field_sel")
        entered = st.number_input(
            "TaxAct amount entered ($, cents ignored — enter dollars.cents)",
            min_value=-1e12,
            max_value=1e12,
            value=0.0,
            step=0.01,
            key="cmp_entered_amt",
        )
        explanation = st.text_input(
            "Explanation (retained with the entry's history)", "", key="cmp_explanation"
        )
        submitted = st.form_submit_button("Record comparison", type="primary")
    if submitted:
        row = required_rows[options.index(choice)]
        entered_cents = round(float(entered) * 100)
        wp_cents = row.working_paper_cents or 0
        delta = entered_cents - wp_cents
        status = "MATCHED" if delta == 0 else "DISCREPANCY"
        note = explanation or (
            "TaxAct matches the reviewed working paper"
            if delta == 0
            else f"Discrepancy of ${abs(delta) / 100:,.2f} — re-check the TaxAct entry."
        )
        tax_svc.record_reconciliation_item(
            workspace_id=ws_id,
            tax_section=row.tax_section,
            field_key=row.field_key,
            source_working_paper_cents=wp_cents,
            taxact_entered_cents=entered_cents,
            delta_cents=delta,
            status=status,
            explanation=note,
        )
        st.success(
            f"Recorded {choice}: working paper ${wp_cents / 100:,.2f}, TaxAct "
            f"${entered_cents / 100:,.2f} — {status}."
        )
        st.rerun()
else:
    st.info("No required fields with evidence yet.")

# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

st.subheader("Export")
st.caption(
    f"Exports are written to a private directory outside the repository "
    f"(default root: {sanitize_path(default_export_root())}). Drafts are "
    "always allowed and labelled; a final export is refused while readiness "
    "or coverage gaps remain."
)
col_draft, col_final = st.columns(2)
draft_path = final_path = None
with col_draft:
    if st.button("Export draft", key="cmp_export_draft"):
        try:
            result = export_packet(session, ws_id, is_draft=True)
            draft_path = result.destination
            st.success(f"Draft exported to {sanitize_path(result.destination)}")
        except TaxExportError as err:
            st.error(f"Draft export failed: {err}")
with col_final:
    if st.button("Export final packet", type="primary", key="cmp_export_final"):
        try:
            result = export_packet(session, ws_id, is_draft=False)
            final_path = result.destination
            st.success(
                f"Final packet exported to {sanitize_path(result.destination)} "
                f"(content hash {result.content_hash[:12]}…)"
            )
        except ExportReadinessError as err:
            st.error(str(err))
        except TaxExportError as err:
            st.error(f"Export failed: {err}")
if draft_path or final_path:
    st.info("Use the workspace service or export module to verify an export is still current later.")
