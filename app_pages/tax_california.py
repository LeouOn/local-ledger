"""California Tax Differences, Sourcing, and Carryovers UI (TAX-15).

Provides:
1. Taxpayer residency and property location sourcing facts review.
2. Separate federal vs. California asset depreciation tracking (FTB 3885A vs Form 4562).
3. Federal vs. California prior-year carryover tracking (Schedule D / FTB 3801).
4. Schedule CA (540) adjustments summary (Column B subtractions & Column C additions).
5. TaxAct comparison items generation.
6. Official 2025 California statutory authority registry.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd
import streamlit as st
from sqlmodel import select

from local_ledger.tax.california import (
    AdjustmentType,
    CaliforniaTaxService,
)
from local_ledger.tax.models import (
    Jurisdiction,
    ResidencyStatus,
    TaxProperty,
    WorkspaceStatus,
)
from local_ledger.tax.service import TaxWorkspaceService
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import kpi_card, section_header

section_header("CALIFORNIA TAX REVIEW", "Federal / California Differences & Carryovers")

session = get_session()
tax_svc = TaxWorkspaceService(session)
ca_svc = CaliforniaTaxService(session)

# ---------------------------------------------------------------------------
# Workspace Selector
# ---------------------------------------------------------------------------
col_year, col_jur = st.columns([1, 1])
with col_year:
    tax_year_str = st.selectbox("Tax Year", ["2025", "2024", "2026"], index=0, key="ca_tax_year_sel")
    tax_year = int(tax_year_str)

with col_jur:
    jurisdiction = st.selectbox(
        "Workspace Jurisdiction",
        [Jurisdiction.CALIFORNIA, Jurisdiction.FEDERAL],
        index=0,
        format_func=lambda j: "California (US-CA)" if j == Jurisdiction.CALIFORNIA else "Federal (US-FED)",
        key="ca_tax_jur_sel",
    )

workspace = tax_svc.get_or_create_workspace(tax_year=tax_year, jurisdiction=jurisdiction)
is_locked = workspace.status == WorkspaceStatus.LOCKED

if is_locked:
    st.warning(f"🔒 Tax Workspace {tax_year} ({jurisdiction}) is LOCKED and immutable. Edits are disabled.")

# ---------------------------------------------------------------------------
# Analysis & KPIs
# ---------------------------------------------------------------------------
res_analysis = ca_svc.analyze_residency_and_sourcing(workspace.id)
ca_report = ca_svc.compute_schedule_ca_adjustments(workspace.id)

kpi_cols = st.columns(4)
with kpi_cols[0]:
    tone: Literal["ok", "warn", "bad", "neutral"] = (
        "ok"
        if res_analysis.is_determined
        else "bad"
    )
    kpi_card(
        "Residency Status",
        workspace.residency_status,
        delta="Confirmed" if res_analysis.is_determined else "Action required",
        status=tone,
    )
with kpi_cols[1]:
    kpi_card(
        "Schedule CA Subtractions (Col B)",
        f"${ca_report.total_subtractions_col_b_cents / 100:,.2f}",
        delta=f"{len([a for a in ca_report.adjustments if a.subtraction_col_b_cents > 0])} item(s)",
        status="ok",
    )
with kpi_cols[2]:
    kpi_card(
        "Schedule CA Additions (Col C)",
        f"${ca_report.total_additions_col_c_cents / 100:,.2f}",
        delta=f"{len([a for a in ca_report.adjustments if a.addition_col_c_cents > 0])} item(s)",
        status="ok",
    )
with kpi_cols[3]:
    net_val = ca_report.net_california_adjustment_cents
    prefix = "+" if net_val > 0 else ("-" if net_val < 0 else "")
    kpi_card(
        "Net CA Adjustment",
        f"{prefix}${abs(net_val) / 100:,.2f}",
        delta="Col C - Col B",
        status="ok" if ca_report.is_ready_for_review else "warn",
    )

st.divider()

# ---------------------------------------------------------------------------
# Tabs Navigation
# ---------------------------------------------------------------------------
tab_res, tab_dep, tab_cry, tab_ca, tab_auth = st.tabs(
    [
        "🌴 Residency & Sourcing",
        "📉 Depreciation Differences",
        "🔄 Prior-Year Carryovers",
        "📑 Schedule CA (540)",
        "⚖️ Authority Registry",
    ]
)

# ---------------------------------------------------------------------------
# Tab 1: Residency & Sourcing
# ---------------------------------------------------------------------------
with tab_res:
    st.subheader("Taxpayer Residency & Real Property Sourcing")
    st.caption(
        "California requires confirming residency facts before applying resident or nonresident mappings. "
        "Full-year residents are taxed on worldwide income; nonresidents are taxed solely on California-source income."
    )

    if not res_analysis.is_determined:
        st.error(
            "⚠️ **Residency is UNKNOWN.** California tax rules cannot be verified or applied until "
            "residency is confirmed. Out-of-state income sourcing cannot be determined."
        )
    elif workspace.residency_status == ResidencyStatus.PART_YEAR:
        st.warning(
            "⚠️ **Part-Year Resident:** Requires dual allocation of income earned while resident vs nonresident (Form 540NR)."
        )
    else:
        st.success(f"✅ Residency confirmed as **{workspace.residency_status}**.")

    st.info(f"📌 **Sourcing Rule:** {res_analysis.sourcing_summary}")

    # Form to update confirmed residency status
    if not is_locked:
        with st.expander("Update / Confirm Residency Status"):
            with st.form("update_residency_form"):
                new_res = st.selectbox(
                    "Select Confirmed Residency Status",
                    [
                        ResidencyStatus.FULL_YEAR,
                        ResidencyStatus.NON_RESIDENT,
                        ResidencyStatus.PART_YEAR,
                        ResidencyStatus.UNKNOWN,
                    ],
                    index=(
                        [
                            ResidencyStatus.FULL_YEAR,
                            ResidencyStatus.NON_RESIDENT,
                            ResidencyStatus.PART_YEAR,
                            ResidencyStatus.UNKNOWN,
                        ].index(workspace.residency_status)
                        if workspace.residency_status in [
                            ResidencyStatus.FULL_YEAR,
                            ResidencyStatus.NON_RESIDENT,
                            ResidencyStatus.PART_YEAR,
                            ResidencyStatus.UNKNOWN,
                        ]
                        else 3
                    ),
                )
                submitted_res = st.form_submit_button("Save Residency Status")
                if submitted_res:
                    workspace.residency_status = new_res
                    session.add(workspace)
                    session.commit()
                    session.refresh(workspace)
                    st.success(f"Updated residency status to {new_res}.")
                    st.rerun()

    # Real Property Sourcing Table
    st.markdown("##### Rental Real Estate Property Sourcing")
    if not res_analysis.property_sourcing:
        st.info("No rental properties registered in this workspace yet.")
    else:
        prop_rows = []
        for p_info in res_analysis.property_sourcing.values():
            prop_rows.append(
                {
                    "Property": p_info["address"],
                    "State": p_info["state"],
                    "In California": "Yes" if p_info["is_california_property"] else "No",
                    "CA Sourced %": f"{p_info['allocation_percentage']}%",
                    "Legal Guidance": p_info["notes"],
                }
            )
        prop_df = pd.DataFrame(prop_rows)
        st.dataframe(prop_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Tab 2: Depreciation Differences (FTB 3885A)
# ---------------------------------------------------------------------------
with tab_dep:
    st.subheader("Asset Depreciation & Basis Differences (FTB 3885A vs Form 4562)")
    st.caption(
        "California does not conform to federal bonus depreciation under IRC § 168(k). "
        "Assets claiming bonus depreciation federally must be adjusted on California Form FTB 3885A and Schedule CA (540)."
    )

    dep_diffs = ca_report.depreciation_differences
    if not dep_diffs:
        st.info("No depreciable assets recorded in this workspace yet.")
    else:
        dep_rows = []
        for d in dep_diffs:
            col_str = "Col B (Subtraction)" if d.schedule_ca_column == AdjustmentType.SUBTRACTION_COL_B else (
                "Col C (Addition)" if d.schedule_ca_column == AdjustmentType.ADDITION_COL_C else "No Difference"
            )
            dep_rows.append(
                {
                    "Asset": d.description,
                    "Property": d.property_address,
                    "Basis": f"${d.depreciable_basis_cents / 100:,.2f}",
                    "Fed 2025 Dep": f"${d.federal_current_depreciation_cents / 100:,.2f}",
                    "CA 2025 Dep": f"${d.california_current_depreciation_cents / 100:,.2f}",
                    "Difference": f"${d.current_year_delta_cents / 100:,.2f}",
                    "Schedule CA": col_str,
                    "Adjustment": f"${d.adjustment_amount_cents / 100:,.2f}",
                    "Fed Remaining": f"${d.federal_remaining_basis_cents / 100:,.2f}",
                    "CA Remaining": f"${d.california_remaining_basis_cents / 100:,.2f}",
                }
            )
        st.dataframe(pd.DataFrame(dep_rows), use_container_width=True, hide_index=True)

    # Form to add a depreciable asset
    if not is_locked:
        with st.expander("Record Depreciable Asset Schedule"):
            properties = list(
                session.exec(select(TaxProperty).where(TaxProperty.workspace_id == workspace.id)).all()
            )
            if not properties:
                st.warning("Please create a rental property on the Intake or Settings page first.")
            else:
                with st.form("record_asset_form", clear_on_submit=True):
                    prop_sel = st.selectbox(
                        "Rental Property",
                        properties,
                        format_func=lambda p: f"{p.street_address}, {p.city}",
                    )
                    desc_val = st.text_input("Asset Description", placeholder="e.g. Refrigerator, Roof, Structure")
                    date_val = st.text_input("Date Placed in Service (YYYY-MM-DD)", value="2025-01-15")

                    col_c1, col_c2 = st.columns(2)
                    with col_c1:
                        cost_dlr = st.number_input("Cost Basis ($)", min_value=0.00, value=3000.00, step=100.0)
                        land_dlr = st.number_input("Land Value (Non-depreciable) ($)", min_value=0.00, value=0.00, step=100.0)
                    with col_c2:
                        recovery_mos = st.selectbox(
                            "Recovery Period",
                            [330, 60, 84, 180, 360],
                            format_func=lambda m: f"{m / 12:.1f} years ({m} months)",
                        )
                        diff_reason = st.selectbox(
                            "State Difference Reason",
                            [
                                "Bonus depreciation non-conformity (IRC § 168(k) vs FTB 3885A)",
                                "Standard MACRS vs CA Modified S/L",
                                "Residential Structure S/L 27.5-year (Conforming)",
                                "Other State Difference",
                            ],
                        )

                    st.markdown("##### 2025 Depreciation Figures")
                    col_d1, col_d2 = st.columns(2)
                    with col_d1:
                        prior_fed_dlr = st.number_input("Prior Fed Depreciation ($)", min_value=0.00, value=0.00, step=50.0)
                        curr_fed_dlr = st.number_input("Current 2025 Fed Depreciation ($)", min_value=0.00, value=0.00, step=50.0)
                    with col_d2:
                        prior_ca_dlr = st.number_input("Prior CA Depreciation ($)", min_value=0.00, value=0.00, step=50.0)
                        curr_ca_dlr = st.number_input("Current 2025 CA Depreciation ($)", min_value=0.00, value=0.00, step=50.0)

                    submitted_asset = st.form_submit_button("Record Asset Schedule")
                    if submitted_asset:
                        if not desc_val.strip():
                            st.error("Asset description is required.")
                        else:
                            ca_svc.record_depreciable_asset(
                                workspace_id=workspace.id,
                                property_id=prop_sel.id,
                                description=desc_val.strip(),
                                date_placed_in_service=date_val.strip(),
                                cost_basis_cents=round(cost_dlr * 100),
                                land_value_cents=round(land_dlr * 100),
                                recovery_period_months=recovery_mos,
                                federal_prior_depreciation_cents=round(prior_fed_dlr * 100),
                                california_prior_depreciation_cents=round(prior_ca_dlr * 100),
                                federal_current_depreciation_cents=round(curr_fed_dlr * 100),
                                california_current_depreciation_cents=round(curr_ca_dlr * 100),
                                state_difference_reason=diff_reason,
                            )
                            st.success(f"Recorded asset schedule for '{desc_val}'.")
                            st.rerun()

# ---------------------------------------------------------------------------
# Tab 3: Prior-Year Carryovers (Schedule D & FTB 3801)
# ---------------------------------------------------------------------------
with tab_cry:
    st.subheader("Federal vs. California Prior-Year Carryovers")
    st.caption(
        "Tracks capital loss carryovers (Form 1040 Schedule D line 14 vs FTB Form 540 Schedule D line 6) "
        "and suspended passive rental loss carryovers (Form 8582 vs FTB 3801)."
    )

    carryovers = ca_report.carryover_comparisons
    if not carryovers:
        st.info("No prior-year carryovers recorded for this workspace yet.")
    else:
        cry_rows = []
        for c in carryovers:
            has_missing = c.has_missing_prior_return
            fed_prior_str = f"${c.federal_prior_year_cents / 100:,.2f}" if c.federal_prior_year_cents is not None else "MISSING"
            ca_prior_str = f"${c.california_prior_year_cents / 100:,.2f}" if c.california_prior_year_cents is not None else "MISSING"
            delta_str = f"${c.prior_year_carryover_delta_cents / 100:,.2f}" if c.prior_year_carryover_delta_cents is not None else "N/A"

            cry_rows.append(
                {
                    "Category": c.category,
                    "Federal Prior Return": fed_prior_str,
                    "CA Prior Return": ca_prior_str,
                    "Carryover Delta": delta_str,
                    "Fed 2025 Utilized": f"${c.federal_utilized_cents / 100:,.2f}",
                    "CA 2025 Utilized": f"${c.california_utilized_cents / 100:,.2f}",
                    "Status": c.status,
                    "Citation": c.authority_citation,
                }
            )
        st.dataframe(pd.DataFrame(cry_rows), use_container_width=True, hide_index=True)

        for c in carryovers:
            if c.has_missing_prior_return:
                st.warning(
                    f"⚠️ **{c.category} Missing Prior Return Evidence:** Carryover cannot be verified or "
                    "assumed to be 0 without 2024 tax return documentation."
                )

    # Form to record carryover
    if not is_locked:
        with st.expander("Record Prior-Year Carryover"):
            with st.form("record_carryover_form", clear_on_submit=True):
                col_cat, col_jur = st.columns(2)
                with col_cat:
                    cry_cat = st.selectbox("Carryover Category", ["CAPITAL_LOSS", "PASSIVE_ACTIVITY_LOSS"])
                with col_jur:
                    cry_jur = st.selectbox(
                        "Jurisdiction",
                        [Jurisdiction.CALIFORNIA, Jurisdiction.FEDERAL],
                        format_func=lambda j: "California (US-CA)" if j == Jurisdiction.CALIFORNIA else "Federal (US-FED)",
                    )

                is_missing = st.checkbox("Prior-year return missing / undetermined", value=False)
                prior_amt = st.number_input("Prior Year Carryover ($)", min_value=0.00, value=7500.00, step=100.0, disabled=is_missing)
                util_amt = st.number_input("Current 2025 Utilized Amount ($)", min_value=0.00, value=3000.00, step=100.0)
                ref_str = st.text_input("Prior Return Form & Line Reference", placeholder="e.g. 2024 Form 540 Schedule D line 6")

                submitted_cry = st.form_submit_button("Record Carryover")
                if submitted_cry:
                    prior_cents = None if is_missing else round(prior_amt * 100)
                    ca_svc.record_carryover(
                        workspace_id=workspace.id,
                        jurisdiction=cry_jur,
                        category=cry_cat,
                        prior_year_cents=prior_cents,
                        utilized_current_year_cents=round(util_amt * 100),
                        prior_return_reference=ref_str.strip(),
                    )
                    st.success(f"Recorded {cry_jur} {cry_cat} carryover.")
                    st.rerun()

# ---------------------------------------------------------------------------
# Tab 4: Schedule CA (540) Reconciliation
# ---------------------------------------------------------------------------
with tab_ca:
    st.subheader("California Schedule CA (540) Adjustments Summary")
    st.caption(
        "Reconciles federal AGI adjustments required under California law. "
        "Column B represents subtractions from federal income; Column C represents additions."
    )

    if not ca_report.adjustments:
        st.info("No California statutory adjustments identified for this workspace.")
    else:
        ca_rows = []
        for a in ca_report.adjustments:
            ca_rows.append(
                {
                    "Line Item": a.schedule_ca_line,
                    "Description": a.title,
                    "Col B (Subtractions)": f"${a.subtraction_col_b_cents / 100:,.2f}",
                    "Col C (Additions)": f"${a.addition_col_c_cents / 100:,.2f}",
                    "Net Adjustment": f"${a.net_adjustment_cents / 100:,.2f}",
                    "Authority Citation": a.authority_citation,
                }
            )
        st.dataframe(pd.DataFrame(ca_rows), use_container_width=True, hide_index=True)

        col_tot1, col_tot2, col_tot3 = st.columns(3)
        with col_tot1:
            st.metric("Total Subtractions (Col B)", f"${ca_report.total_subtractions_col_b_cents / 100:,.2f}")
        with col_tot2:
            st.metric("Total Additions (Col C)", f"${ca_report.total_additions_col_c_cents / 100:,.2f}")
        with col_tot3:
            st.metric("Net Schedule CA Adjustment", f"${ca_report.net_california_adjustment_cents / 100:,.2f}")

    if ca_report.unresolved_questions:
        st.warning("⚠️ **Unresolved Items Blocking Final Verification:**")
        for u in ca_report.unresolved_questions:
            st.markdown(f"- {u}")

    st.divider()

    # TaxAct Comparison generation
    if not is_locked and st.button("Generate TaxAct Reconciliation Items", type="primary"):
        items = ca_svc.generate_taxact_reconciliation_items(workspace.id)
        st.success(f"Generated {len(items)} reconciliation line item(s) for TaxAct comparison!")
        st.rerun()

# ---------------------------------------------------------------------------
# Tab 5: Authority Registry
# ---------------------------------------------------------------------------
with tab_auth:
    st.subheader("Official 2025 California Statutory Authorities")
    st.caption(
        "Seeded legal references from the California Revenue and Taxation Code (R&TC), "
        "Franchise Tax Board (FTB) instructions, and federal nonconformity guidelines."
    )

    auths = ca_svc.get_all_authorities()
    for auth in auths:
        with st.expander(f"{auth.title} — {auth.statutory_authority}"):
            st.markdown(f"**FTB Form:** `{auth.ftb_form}` | **Federal Counterpart:** `{auth.irs_form}`")
            st.markdown(f"**Schedule CA Line:** `{auth.schedule_ca_line}`")
            st.markdown(f"**Adjustment Type:** `{auth.adjustment_type}`")
            st.markdown(f"**Legal Summary:** {auth.summary}")
            st.caption(f"Sources: {auth.official_citation_notes}")
