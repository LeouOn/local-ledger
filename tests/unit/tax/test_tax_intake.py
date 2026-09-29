"""Unit tests for Tax Document Intake and Verification (TAX-05).

Validates:
1. Expected vs. missing document inventory checklist tracking.
2. Safe local file intake: format validation, size limit (25MB), empty file rejection.
3. Content-addressable SHA-256 deduplication: duplicate uploads return existing doc without duplicating approved amounts.
4. Text PDF page-by-page extraction vs. scanned PDF detection (scanned_no_text).
5. Manual field entry proposal and review lifecycle (VERIFY, REJECT).
6. Atomicity and zero half-registered records on parsing/validation failures.
7. Supersession lifecycle: corrected uploads invalidate downstream items.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.tax import (
    DocumentType,
    EmptyFileError,
    EvidenceStatus,
    FileOversizedError,
    Jurisdiction,
    ScanState,
    TaxDocument,
    TaxWorkspaceService,
    UnsupportedFileTypeError,
    add_manual_field,
    get_inventory_status,
    get_workspace_fields_summary,
    ingest_document,
    inspect_file_content,
    review_field,
    validate_file_upload,
)

# Structurally valid single-page PDF with embedded text layer
_TEXT_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 50>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (CONSOLIDATED 1099 DIVIDENDS 1250) Tj ET\nendstream endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)

# Structurally valid PDF without text layer (representing scanned PDF)
_SCANNED_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def tax_service(session: Session, engine) -> TaxWorkspaceService:
    init_db(engine)
    return TaxWorkspaceService(session)


def test_inventory_tracking_missing_and_present(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Verify expected inventory tracks required vs optional and missing vs present."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    # Initially, all standard expected documents are MISSING
    initial_inv = get_inventory_status(session, ws.id)
    assert len(initial_inv) >= 8

    req_items = [inv for inv in initial_inv if inv.is_required]
    assert len(req_items) >= 4  # 1099-Consolidated, Rental statement, 1098, Property tax bill
    for item in req_items:
        assert item.status == "MISSING"
        assert item.active_document_count == 0

    # Ingest a 1099-Consolidated
    res = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="brokerage_1099.pdf",
        file_bytes=_TEXT_PDF,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    assert res.success is True
    assert res.document is not None

    # Inventory updates: 1099-Consolidated should now be PRESENT
    updated_inv = get_inventory_status(session, ws.id)
    doc_1099 = next(i for i in updated_inv if i.doc_type == DocumentType.FORM_1099_CONSOLIDATED)
    assert doc_1099.status == "PRESENT"
    assert doc_1099.active_document_count == 1
    assert len(doc_1099.documents) == 1


def test_custom_document_inventory(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Ingesting a custom document type lists it in the inventory."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="roof_repair_invoice.pdf",
        file_bytes=_TEXT_PDF,
        doc_type="CUSTOM_ROOF_INVOICE",
    )

    inv = get_inventory_status(session, ws.id)
    custom_entry = next((i for i in inv if i.doc_type == "CUSTOM_ROOF_INVOICE"), None)
    assert custom_entry is not None
    assert custom_entry.status == "PRESENT"
    assert custom_entry.active_document_count == 1


def test_file_validation_oversized_and_empty() -> None:
    """Validate empty file and oversized file rejection."""
    # Empty file
    with pytest.raises(EmptyFileError):
        validate_file_upload("test.pdf", b"")

    # Oversized file (>25MB)
    huge_bytes = b"0" * (25 * 1024 * 1024 + 1)
    with pytest.raises(FileOversizedError):
        validate_file_upload("huge.pdf", huge_bytes)

    # Unsupported file extension
    with pytest.raises(UnsupportedFileTypeError):
        validate_file_upload("malicious.exe", b"MZexecutable")


def test_ingest_handles_validation_failure_atomically(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Validation errors return success=False and leave zero records in DB."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    res_empty = ingest_document(session, ws.id, "empty.pdf", b"")
    assert res_empty.success is False
    assert "empty" in res_empty.error_message.lower()

    res_invalid_type = ingest_document(session, ws.id, "script.sh", b"echo hello")
    assert res_invalid_type.success is False
    assert "unsupported" in res_invalid_type.error_message.lower()

    # Verify no documents created in DB
    docs = list(session.exec(select(TaxDocument).where(TaxDocument.workspace_id == ws.id)).all())
    assert len(docs) == 0


def test_duplicate_upload_deduplication(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Uploading identical content returns existing document without duplicate records or approved amounts."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    res1 = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="statement.pdf",
        file_bytes=_TEXT_PDF,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    assert res1.success is True
    assert res1.is_duplicate is False
    original_id = res1.document.id

    # Add a verified field to original document
    field1 = add_manual_field(
        session=session,
        workspace_id=ws.id,
        document_id=original_id,
        field_name="1099-DIV:dividends",
        raw_text="1250.00",
        amount_cents=125000,
    )
    review_field(session, field1.id, "VERIFY", reviewer="Auditor")

    summary_before = get_workspace_fields_summary(session, ws.id)
    assert summary_before["verified_cents"] == 125000

    # Ingest the exact same file bytes again
    res2 = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="statement_copy.pdf",
        file_bytes=_TEXT_PDF,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    assert res2.success is True
    assert res2.is_duplicate is True
    assert res2.document.id == original_id

    # Document count in DB must remain 1
    docs = list(session.exec(select(TaxDocument).where(TaxDocument.workspace_id == ws.id)).all())
    assert len(docs) == 1

    # Verified totals must NOT be duplicated
    summary_after = get_workspace_fields_summary(session, ws.id)
    assert summary_after["verified_cents"] == 125000
    assert summary_after["verified_count"] == 1


def test_text_pdf_vs_scanned_pdf_inspection() -> None:
    """Verify text stream parsing and scanned document detection."""
    # Text PDF
    scan_state_text, pages_text, text_dict = inspect_file_content(_TEXT_PDF, "sample.pdf")
    assert scan_state_text == ScanState.TEXT_EXTRACTABLE
    assert pages_text == 1
    assert "CONSOLIDATED 1099 DIVIDENDS" in text_dict[1]

    # Scanned PDF (no text stream)
    scan_state_scanned, pages_scanned, scanned_dict = inspect_file_content(_SCANNED_PDF, "scan.pdf")
    assert scan_state_scanned == ScanState.SCANNED_NO_TEXT
    assert pages_scanned == 1
    assert "no embedded text layer detected" in scanned_dict[1]

    # Image file
    scan_state_img, _, img_dict = inspect_file_content(b"PNGFAKEBYTES", "receipt.png")
    assert scan_state_img == ScanState.SCANNED_NO_TEXT
    assert "Image document" in img_dict[1]

    # Plain text / CSV
    csv_bytes = b"Date,Amount,Payee\n2025-01-01,500.00,Tenant\n"
    scan_state_csv, _, csv_dict = inspect_file_content(csv_bytes, "records.csv")
    assert scan_state_csv == ScanState.TEXT_EXTRACTABLE
    assert "Tenant" in csv_dict[1]


def test_manual_field_entry_and_review_lifecycle(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Validate manual field proposal, verification, and rejection."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    res = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="1098_mortgage.pdf",
        file_bytes=_TEXT_PDF,
        doc_type=DocumentType.FORM_1098_MORTGAGE,
    )
    doc_id = res.document.id

    # 1. Propose manual field
    field_record = add_manual_field(
        session=session,
        workspace_id=ws.id,
        document_id=doc_id,
        field_name="1098:box_1_mortgage_interest",
        raw_text="$8,450.25",
        amount_cents=845025,
        page_number=1,
        box_or_line="Box 1",
        snippet_text="Box 1 Mortgage interest received: $8,450.25",
        notes="Transcribed from lender statement",
    )
    assert field_record.status == EvidenceStatus.PROPOSED
    assert field_record.amount_cents == 845025
    assert field_record.notes == "Transcribed from lender statement"

    # Approval Invariant: PROPOSED field must NOT enter verified totals
    summary1 = get_workspace_fields_summary(session, ws.id)
    assert summary1["verified_cents"] == 0
    assert summary1["proposed_cents"] == 845025
    assert summary1["proposed_count"] == 1
    assert summary1["verified_count"] == 0

    # 2. Verify field
    verified_field = review_field(
        session=session,
        field_id=field_record.id,
        action="VERIFY",
        reviewer="TaxPreparer",
        notes="Confirmed against Box 1",
    )
    assert verified_field.status == EvidenceStatus.VERIFIED
    assert verified_field.verified_by == "TaxPreparer"
    assert verified_field.verified_at is not None

    # Now verified field enters approved totals
    summary2 = get_workspace_fields_summary(session, ws.id)
    assert summary2["verified_cents"] == 845025
    assert summary2["proposed_cents"] == 0
    assert summary2["verified_count"] == 1

    # 3. Propose another field and reject it
    field_bad = add_manual_field(
        session=session,
        workspace_id=ws.id,
        document_id=doc_id,
        field_name="1098:points_paid",
        raw_text="$500.00",
        amount_cents=50000,
        page_number=1,
        box_or_line="Box 6",
    )
    rejected_field = review_field(
        session=session,
        field_id=field_bad.id,
        action="REJECT",
        reviewer="TaxPreparer",
        notes="Points were already deducted in 2024",
    )
    assert rejected_field.status == EvidenceStatus.REJECTED

    # Rejected field does NOT enter verified totals
    summary3 = get_workspace_fields_summary(session, ws.id)
    assert summary3["verified_cents"] == 845025
    assert summary3["rejected_cents"] == 50000
    assert summary3["rejected_count"] == 1


def test_superseded_document_updates_inventory_and_invalidates_fields(
    session: Session, tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Corrected document upload marks superseded document and fields as SUPERSEDED."""
    ws = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    # Ingest original document
    res_orig = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="original_statement.pdf",
        file_bytes=_TEXT_PDF,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    orig_doc_id = res_orig.document.id

    # Propose and verify a field on the original document
    f1 = add_manual_field(
        session=session,
        workspace_id=ws.id,
        document_id=orig_doc_id,
        field_name="1099-INT:interest",
        raw_text="300.00",
        amount_cents=30000,
    )
    review_field(session, f1.id, "VERIFY", reviewer="Auditor")

    summary_before = get_workspace_fields_summary(session, ws.id)
    assert summary_before["verified_cents"] == 30000

    # Ingest corrected replacement document
    corrected_pdf = _TEXT_PDF + b"%CORRECTION REV 2\n"
    res_corr = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="corrected_statement.pdf",
        file_bytes=corrected_pdf,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
        is_corrected=True,
        supersedes_doc_id=orig_doc_id,
    )
    assert res_corr.success is True
    assert res_corr.document.is_corrected is True
    assert res_corr.document.supersedes_doc_id == orig_doc_id

    # Inventory: 1 active document (the correction)
    inv = get_inventory_status(session, ws.id)
    cat_1099 = next(i for i in inv if i.doc_type == DocumentType.FORM_1099_CONSOLIDATED)
    assert cat_1099.status == "PRESENT"
    assert cat_1099.active_document_count == 1
    assert len(cat_1099.documents) == 2

    # Downstream field f1 must now be SUPERSEDED and excluded from verified totals
    session.refresh(f1)
    assert f1.status == EvidenceStatus.SUPERSEDED

    summary_after = get_workspace_fields_summary(session, ws.id)
    assert summary_after["verified_cents"] == 0
    assert summary_after["superseded_cents"] == 30000
    assert summary_after["superseded_count"] == 1
