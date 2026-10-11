"""Tax Document Intake and Manual Verification UI (TAX-05).

Provides:
1. Expected vs. missing document inventory checklist for 2025 review scope.
2. Safe local file intake: strict format, size validation, and SHA-256 deduplication.
3. Document inspection with page-by-page text stream preview and scanned PDF detection.
4. Field verification review queue enforcing the approval invariant (only VERIFIED in totals).
5. Manual field entry for scanned or unparsed source evidence.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd
import streamlit as st
from sqlmodel import select

from local_ledger.llm.providers import load_settings
from local_ledger.models import Account
from local_ledger.tax.extraction import local_client_and_model, propose_for_documents
from local_ledger.tax.intake import (
    EXPECTED_2025_CATEGORIES,
    add_manual_field,
    get_inventory_status,
    get_workspace_fields_summary,
    ingest_document,
    inspect_file_content,
    review_field,
)
from local_ledger.tax.models import (
    EvidenceStatus,
    ExtractedField,
    Jurisdiction,
    ScanState,
    TaxDocument,
    TaxProperty,
    WorkspaceStatus,
)
from local_ledger.tax.scope import ReviewScopeMode, WorkspaceScopeService
from local_ledger.tax.service import TaxWorkspaceService
from local_ledger.tax.storage import sanitize_path, verify_stored_document
from local_ledger.ui.chrome import get_session
from local_ledger.ui.components import kpi_card, section_header, status_badge

section_header("TAX PREPARATION", "Document Intake & Verification")

session = get_session()
tax_svc = TaxWorkspaceService(session)

# ---------------------------------------------------------------------------
# Workspace Selector
# ---------------------------------------------------------------------------
col_year, col_jur = st.columns([1, 1])
with col_year:
    tax_year_str = st.selectbox("Tax Year", ["2025", "2024", "2026"], index=0, key="tax_year_sel")
    tax_year = int(tax_year_str)

with col_jur:
    jurisdiction = st.selectbox(
        "Jurisdiction",
        [Jurisdiction.FEDERAL, Jurisdiction.CALIFORNIA],
        index=0,
        format_func=lambda j: "Federal (US-FED)" if j == Jurisdiction.FEDERAL else "California (US-CA)",
        key="tax_jur_sel",
    )

workspace = tax_svc.get_or_create_workspace(tax_year=tax_year, jurisdiction=jurisdiction)
is_locked = workspace.status == WorkspaceStatus.LOCKED

if is_locked:
    st.warning(f"🔒 Tax Workspace {tax_year} ({jurisdiction}) is LOCKED and immutable. Changes are disabled.")

scope_svc = WorkspaceScopeService(session)
scope = scope_svc.get(workspace.id)
with st.expander("Review scope", expanded=not scope.is_configured):
    st.caption("Choose which accounts and properties this review covers. Documents only skips bank-flow checks.")
    accounts = list(session.exec(select(Account).order_by(Account.name)).all())
    properties = list(session.exec(select(TaxProperty).where(TaxProperty.workspace_id == workspace.id)).all())
    with st.form(f"tax_scope_{workspace.id}"):
        mode = st.selectbox(
            "Review mode", list(ReviewScopeMode), index=list(ReviewScopeMode).index(scope.mode),
            format_func=lambda m: {
                ReviewScopeMode.UNCONFIGURED: "Not configured",
                ReviewScopeMode.DOCUMENTS_ONLY: "Documents only",
                ReviewScopeMode.SELECTED_ACCOUNTS: "Selected accounts and documents",
            }[m], disabled=is_locked,
        )
        account_ids = st.multiselect(
            "Accounts", [a.id for a in accounts], default=scope.account_ids,
            format_func=lambda aid: next(a.name for a in accounts if a.id == aid), disabled=is_locked,
        )
        property_ids = st.multiselect(
            "Rental properties", [p.id for p in properties], default=scope.property_ids,
            format_func=lambda pid: next(p.street_address for p in properties if p.id == pid), disabled=is_locked,
        )
        confirmed = st.checkbox(
            "I have confirmed this property list, including an intentionally empty list",
            value=scope.properties_confirmed, disabled=is_locked,
        )
        if st.form_submit_button("Save review scope", disabled=is_locked):
            try:
                scope_svc.configure(
                    workspace_id=workspace.id, mode=mode,
                    account_ids=account_ids if mode == ReviewScopeMode.SELECTED_ACCOUNTS else [],
                    property_ids=property_ids, properties_confirmed=confirmed,
                )
                st.rerun()
            except Exception as err:
                st.error(f"Could not save review scope: {sanitize_path(str(err))}")

# ---------------------------------------------------------------------------
# Workspace KPIs
# ---------------------------------------------------------------------------
inventory = get_inventory_status(session, workspace.id)
summary = get_workspace_fields_summary(session, workspace.id)

req_total = sum(1 for inv in inventory if inv.is_required)
req_present = sum(1 for inv in inventory if inv.is_required and inv.status == "PRESENT")
req_missing = sum(1 for inv in inventory if inv.is_required and inv.status == "MISSING")

kpi_cols = st.columns(4)
with kpi_cols[0]:
    kpi_card(
        "Required Docs",
        f"{req_present}/{req_total}",
        delta=f"{req_missing} missing" if req_missing > 0 else "All present",
        status="ok" if req_missing == 0 else "warn",
    )
with kpi_cols[1]:
    kpi_card(
        "Verified Fields",
        str(summary["verified_count"]),
        delta="See working papers for totals by type",
        status="ok",
    )
with kpi_cols[2]:
    kpi_card(
        "Proposed Fields",
        str(summary["proposed_count"]),
        delta="Awaiting your review",
        status="warn" if summary["proposed_count"] > 0 else "ok",
    )
with kpi_cols[3]:
    kpi_card(
        "Total Evidence Items",
        str(summary["total_fields"]),
        delta=f"{summary['rejected_count']} rejected / {summary['superseded_count']} superseded",
        status="neutral" if summary["total_fields"] > 0 else "warn",
    )

st.divider()

# ---------------------------------------------------------------------------
# Tabs Navigation
# ---------------------------------------------------------------------------
tab_inv, tab_up, tab_inspect, tab_queue, tab_entry = st.tabs(
    [
        "📋 Document Checklist",
        "📤 Upload Evidence",
        "🔍 Document Inspector",
        "✅ Verification Queue",
        "✏️ Manual Entry",
    ]
)

# ---------------------------------------------------------------------------
# Tab 1: Document Checklist
# ---------------------------------------------------------------------------
with tab_inv:
    st.subheader("Expected 2025 Document Inventory")
    st.caption(
        "Tracking expected source tax forms for 2025 investments and rental property income. "
        "Missing required forms must be ingested before final filing review."
    )

    if req_missing > 0:
        missing_titles = [inv.title for inv in inventory if inv.is_required and inv.status == "MISSING"]
        st.warning(f"⚠️ Missing {len(missing_titles)} required tax document(s): {', '.join(missing_titles)}")
    else:
        st.success("✅ All required 2025 documents are present in this workspace!")

    inv_rows = []
    for inv in inventory:
        badge_tone: Literal["ok", "warn", "bad", "neutral"] = "ok" if inv.status == "PRESENT" else ("warn" if inv.is_required else "neutral")
        if inv.status == "SUPERSEDED":
            badge_tone = "bad"
        inv_rows.append(
            {
                "Category": inv.title,
                "Type Code": inv.doc_type,
                "Required": "Required" if inv.is_required else "Optional",
                "Status": inv.status,
                "Active Files": inv.active_document_count,
                "Description": inv.description,
            }
        )

    inv_df = pd.DataFrame(inv_rows)
    st.dataframe(inv_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Tab 2: Upload Evidence
# ---------------------------------------------------------------------------
with tab_up:
    st.subheader("Safe Local Document Ingestion")
    st.caption(
        "Upload tax documents to local private storage. File size limit: 25 MB. "
        "Original documents are stored immutably with SHA-256 verification outside the git repository."
    )

    if is_locked:
        st.error("Uploads disabled because workspace is locked.")
    else:
        uploaded_file = st.file_uploader(
            "Select Tax Document",
            type=["pdf", "json", "csv", "txt", "png", "jpg", "jpeg"],
            key="tax_doc_uploader",
        )

        doc_type_options = [cat.doc_type for cat in EXPECTED_2025_CATEGORIES]
        doc_type_labels = {cat.doc_type: f"{cat.title} ({cat.doc_type})" for cat in EXPECTED_2025_CATEGORIES}

        selected_doc_type = st.selectbox(
            "Document Category",
            doc_type_options,
            format_func=lambda dt: doc_type_labels.get(dt, dt),
            key="tax_doc_type_sel",
        )

        # Existing active documents in workspace
        existing_docs = list(
            session.exec(
                select(TaxDocument)
                .where(TaxDocument.workspace_id == workspace.id)
                .order_by(TaxDocument.ingested_at.desc())  # type: ignore[attr-defined]
            ).all()
        )
        superseded_ids = {d.supersedes_doc_id for d in existing_docs if d.supersedes_doc_id}
        active_existing_docs = [d for d in existing_docs if d.id not in superseded_ids]

        is_correction = st.checkbox(
            "This document is a replacement / corrected form (supersedes prior upload)",
            value=False,
            key="tax_is_correction",
        )

        supersedes_id: str | None = None
        if is_correction:
            if not active_existing_docs:
                st.info("No prior documents available in this workspace to supersede.")
            else:
                target_doc = st.selectbox(
                    "Select Document to Supersede",
                    active_existing_docs,
                    format_func=lambda d: f"{d.file_name} ({d.doc_type}) - SHA: {d.sha256_hash[:8]}",
                    key="tax_supersedes_sel",
                )
                supersedes_id = target_doc.id if target_doc else None

        if st.button("Ingest Document", type="primary", disabled=uploaded_file is None) and uploaded_file is not None:
            file_bytes = uploaded_file.getvalue()
            res = ingest_document(
                session=session,
                workspace_id=workspace.id,
                file_name=uploaded_file.name,
                file_bytes=file_bytes,
                doc_type=selected_doc_type,
                is_corrected=is_correction,
                supersedes_doc_id=supersedes_id,
            )

            if res.success and res.document:
                if res.is_duplicate:
                    st.warning(
                        f"Duplicate document detected: Identical file already registered as "
                        f"'{res.document.file_name}'. Returned existing record without duplicating approved amounts."
                    )
                else:
                    st.success(
                        f"✅ Document '{res.document.file_name}' ingested successfully. "
                        f"(ID: {res.document.id[:8]}..., SHA-256: {res.document.sha256_hash[:12]}...)"
                    )

                if res.scan_state == ScanState.SCANNED_NO_TEXT:
                    st.warning(
                        "⚠️ Scanned document or image detected with no embedded text layer. "
                        "Please use the 'Manual Entry' tab to record fields from this document."
                    )
                else:
                    st.info(
                        f"📄 Embedded text layer detected across {res.page_count} page(s). "
                        "View extracted text in the 'Document Inspector' tab."
                    )
            else:
                st.error(f"❌ Ingestion failed: {res.error_message}")

# ---------------------------------------------------------------------------
# Tab 3: Document Inspector
# ---------------------------------------------------------------------------
with tab_inspect:
    st.subheader("Source Document & Extracted Text Viewer")
    st.caption("Inspect stored documents, verify on-disk hash integrity, and preview page text.")

    all_ws_docs = list(
        session.exec(
            select(TaxDocument)
            .where(TaxDocument.workspace_id == workspace.id)
            .order_by(TaxDocument.ingested_at.desc())  # type: ignore[attr-defined]
        ).all()
    )

    if not all_ws_docs:
        st.info("No documents have been ingested into this workspace yet.")
    else:
        doc_options = {
            d.id: f"{d.file_name} [{d.doc_type}] ({'CORRECTION' if d.is_corrected else 'ORIGINAL'})"
            for d in all_ws_docs
        }
        selected_doc_id = st.selectbox(
            "Select Document to Inspect",
            list(doc_options.keys()),
            format_func=lambda did: doc_options[did],
            key="inspect_doc_sel",
        )

        inspected_doc = session.get(TaxDocument, selected_doc_id)
        if inspected_doc:
            # Metadata display
            meta_col1, meta_col2, meta_col3 = st.columns(3)
            with meta_col1:
                st.markdown(f"**Filename:** `{inspected_doc.file_name}`")
                st.markdown(f"**Document Type:** `{inspected_doc.doc_type}`")
            with meta_col2:
                st.markdown(f"**Scan State:** `{inspected_doc.scan_state}`")
            with meta_col3:
                st.markdown(f"**SHA-256:** `{inspected_doc.sha256_hash[:16]}...`")
                st.markdown(f"**Pages:** `{inspected_doc.page_count}`")

            # Check if superseded
            superseding_doc = session.exec(
                select(TaxDocument).where(TaxDocument.supersedes_doc_id == inspected_doc.id)
            ).first()
            if superseding_doc:
                st.error(
                    f"⚠️ This document was SUPERSEDED by '{superseding_doc.file_name}' on "
                    f"{superseding_doc.ingested_at.strftime('%Y-%m-%d %H:%M:%S')}. "
                    "Its fields cannot be verified or used in approved totals."
                )

            st.divider()

            # Read and preview text content
            try:
                verified_path = verify_stored_document(
                    inspected_doc.storage_path, inspected_doc.sha256_hash
                )
                file_bytes = verified_path.read_bytes()
                st.caption(f"Original file: {len(file_bytes):,} bytes; SHA-256 verified.")
                st.download_button(
                    "Download original document", file_bytes, file_name=inspected_doc.file_name,
                    key=f"download_original_{inspected_doc.id}",
                )
                active_fields = session.exec(select(ExtractedField).where(
                    ExtractedField.document_id == inspected_doc.id,
                    ExtractedField.status.in_([EvidenceStatus.PROPOSED, EvidenceStatus.VERIFIED]),
                )).all()
                st.caption("Local extraction proposes fields for your review. Check each value against the original before verifying.")
                if active_fields:
                    st.info("This document already has active review fields. Review them in the Verification Queue or enter missing fields manually.")
                report_key = f"tax_extract_report_{workspace.id}_{inspected_doc.id}"
                if st.button(
                    "Extract proposals with local model", key=f"btn_extract_{inspected_doc.id}",
                    disabled=is_locked or superseding_doc is not None or bool(active_fields),
                ):
                    client = None
                    try:
                        client, model = local_client_and_model(load_settings(session))
                        with st.spinner(f"Extracting fields with {model}…"):
                            st.session_state[report_key] = propose_for_documents(
                                tax_svc, workspace.id, [(inspected_doc.id, file_bytes)],
                                client=client, model=model,
                            )
                    except Exception as err:
                        st.error(f"Local extraction failed: {sanitize_path(str(err))}")
                    finally:
                        if client is not None:
                            client.close()
                report = st.session_state.get(report_key)
                if report is not None:
                    st.info(f"{report.written_count} proposed field(s) saved. Open the Verification Queue to review them.")
                    for proposal_set in report.sets:
                        for note in proposal_set.notes:
                            st.info(note)
                        for unresolved in proposal_set.unresolved:
                            st.warning(f"Not proposed: {unresolved.field_name or '?'} — {unresolved.reason}")
                    for error in report.errors:
                        st.error(sanitize_path(error.reason))
                _scan_state, page_count, pages_dict = inspect_file_content(
                    file_bytes, inspected_doc.file_name
                )

                if inspected_doc.scan_state == ScanState.SCANNED_NO_TEXT:
                    st.warning(
                        "⚠️ Scanned document with no embedded text layer. "
                        "Manual field entry is required for fields on this document."
                    )

                st.markdown("##### Extracted Page Content")
                for page_num in sorted(pages_dict.keys()):
                    with st.expander(f"Page {page_num}", expanded=(page_num == 1)):
                        st.text_area(
                            f"Page {page_num} Raw Text",
                            value=pages_dict[page_num],
                            height=180,
                            disabled=True,
                            key=f"preview_p_{inspected_doc.id}_{page_num}",
                        )

            except Exception as exc:
                st.error(f"Error accessing document file: {exc}")

# ---------------------------------------------------------------------------
# Tab 4: Verification Queue
# ---------------------------------------------------------------------------
with tab_queue:
    st.subheader("Field Review & Verification")
    st.info(
        "🛡️ **Approval Invariant:** Only fields in `VERIFIED` status enter approved totals. "
        "`PROPOSED`, `REJECTED`, and `SUPERSEDED` fields are strictly excluded from calculations."
    )

    filter_status = st.selectbox(
        "Filter by Status",
        ["ALL", EvidenceStatus.PROPOSED, EvidenceStatus.VERIFIED, EvidenceStatus.REJECTED, EvidenceStatus.SUPERSEDED],
        index=0,
        key="queue_filter_status",
    )

    queue_stmt = select(ExtractedField).where(ExtractedField.workspace_id == workspace.id)
    if filter_status != "ALL":
        queue_stmt = queue_stmt.where(ExtractedField.status == filter_status)
    queue_stmt = queue_stmt.order_by(ExtractedField.field_name)  # type: ignore[attr-defined]
    fields_list = list(session.exec(queue_stmt).all())

    if not fields_list:
        st.info("No fields match the selected filter.")
    else:
        for f in fields_list:
            doc = session.get(TaxDocument, f.document_id)
            doc_label = doc.file_name if doc else f.document_id[:8]

            tone: Literal["ok", "warn", "bad", "neutral"] = "neutral"
            if f.status == EvidenceStatus.VERIFIED:
                tone = "ok"
            elif f.status == EvidenceStatus.PROPOSED:
                tone = "warn"
            elif f.status == EvidenceStatus.REJECTED:
                tone = "bad"

            with st.container():
                col_info, col_amt, col_badge = st.columns([3, 1, 1])
                with col_info:
                    st.markdown(f"**Field:** `{f.field_name}` | **Box/Line:** `{f.box_or_line or 'N/A'}` | **Page:** `{f.page_number}`")
                    st.caption(f"Source: `{doc_label}` | Raw Text: \"{f.raw_text}\"")
                with col_amt:
                    st.markdown(f"### ${f.amount_cents / 100:,.2f}")
                with col_badge:
                    status_badge(f.status, tone=tone)

                if f.snippet_text:
                    st.text(f"Snippet: {f.snippet_text}")

                if f.notes:
                    st.caption(f"Notes: {f.notes}")

                # If PROPOSED, provide reviewer verification actions
                if f.status == EvidenceStatus.PROPOSED and not is_locked:
                    act_col1, act_col2, act_col3 = st.columns([2, 1, 1])
                    with act_col1:
                        review_notes = st.text_input(
                            "Reviewer Notes",
                            value="",
                            key=f"notes_{f.id}",
                            placeholder="Optional verification/rejection rationale",
                        )
                    with act_col2:
                        if st.button("✅ Verify", key=f"btn_verify_{f.id}", type="primary"):
                            try:
                                review_field(
                                    session=session,
                                    field_id=f.id,
                                    action="VERIFY",
                                    reviewer="Reviewer",
                                    notes=review_notes or "Verified against source document",
                                )
                                st.success("Verified!")
                                st.rerun()
                            except Exception as err:
                                st.error(f"Failed to verify: {err}")
                    with act_col3:
                        if st.button("❌ Reject", key=f"btn_reject_{f.id}"):
                            try:
                                review_field(
                                    session=session,
                                    field_id=f.id,
                                    action="REJECT",
                                    reviewer="Reviewer",
                                    notes=review_notes or "Rejected during manual review",
                                )
                                st.warning("Rejected.")
                                st.rerun()
                            except Exception as err:
                                st.error(f"Failed to reject: {err}")

                elif f.status in {EvidenceStatus.VERIFIED, EvidenceStatus.REJECTED}:
                    st.caption(f"Reviewed by: {f.verified_by or 'Unknown'} at {f.verified_at or 'N/A'}")

                st.divider()

# ---------------------------------------------------------------------------
# Tab 5: Manual Entry
# ---------------------------------------------------------------------------
with tab_entry:
    st.subheader("Manual Field Entry")
    st.caption(
        "Record evidence fields manually from physical forms, scanned PDFs, or unparsed documents. "
        "Newly created fields start in `PROPOSED` status and must be verified in the Verification Queue."
    )

    if is_locked:
        st.error("Manual field entry disabled because workspace is locked.")
    else:
        # Non-superseded documents in workspace
        active_docs = [d for d in all_ws_docs if d.id not in superseded_ids]

        if not active_docs:
            st.warning("Please upload at least one tax document in the 'Upload Evidence' tab first.")
        else:
            with st.form("manual_field_form", clear_on_submit=True):
                target_doc = st.selectbox(
                    "Source Document",
                    active_docs,
                    format_func=lambda d: f"{d.file_name} ({d.doc_type})",
                    key="manual_target_doc",
                )

                field_presets = [
                    "1099-B:proceeds",
                    "1099-B:cost_basis",
                    "1099-INT:box_1_interest",
                    "1099-DIV:box_1a_ordinary_dividends",
                    "1099-DIV:box_1b_qualified_dividends",
                    "1098:box_1_mortgage_interest",
                    "Schedule_E:rents_received",
                    "Schedule_E:advertising",
                    "Schedule_E:repairs",
                    "Schedule_E:insurance",
                    "Schedule_E:property_taxes",
                    "Schedule_E:utilities",
                    "Other (Custom)",
                ]

                preset_choice = st.selectbox("Field Name Preset", field_presets, index=0)
                if preset_choice == "Other (Custom)":
                    field_name_val = st.text_input("Custom Field Identifier", value="Custom:Field")
                else:
                    field_name_val = preset_choice

                col_box, col_page = st.columns([2, 1])
                with col_box:
                    box_or_line_val = st.text_input("Box or Line Number", placeholder="e.g. Box 1d, Line 3")
                with col_page:
                    page_val = st.number_input("Page Number", min_value=1, value=1, step=1)

                col_raw, col_amt = st.columns([1, 1])
                with col_raw:
                    raw_text_val = st.text_input("Verbatim Raw Text", placeholder="e.g. $1,450.00")
                with col_amt:
                    amount_dollars = st.number_input(
                        "Amount in Dollars ($)",
                        value=0.00,
                        step=0.01,
                        format="%.2f",
                    )

                snippet_val = st.text_area("Context / Text Snippet", placeholder="Nearby text for citation and audit")
                notes_val = st.text_input("Notes", placeholder="Optional reviewer notes")

                submitted = st.form_submit_button("Propose Field", type="primary")

                if submitted:
                    if not target_doc:
                        st.error("Target document is required.")
                    elif not raw_text_val.strip():
                        st.error("Verbatim raw text cannot be empty.")
                    else:
                        cents_val = round(amount_dollars * 100)
                        try:
                            new_field = add_manual_field(
                                session=session,
                                workspace_id=workspace.id,
                                document_id=target_doc.id,
                                field_name=field_name_val,
                                raw_text=raw_text_val,
                                amount_cents=cents_val,
                                page_number=int(page_val),
                                box_or_line=box_or_line_val,
                                snippet_text=snippet_val,
                                notes=notes_val or None,
                            )
                            st.success(
                                f"✅ Proposed field '{new_field.field_name}' (${cents_val / 100:,.2f})! "
                                "Review and verify it in the 'Verification Queue' tab."
                            )
                        except Exception as err:
                            st.error(f"Failed to record manual field: {err}")
