"""Unit tests for TaxWorkspaceService (TAX-04).

Validates:
1. Approval invariant (only VERIFIED values enter approved totals).
2. Year and workspace isolation.
3. Private document storage, hashing, and immutability.
4. Document correction supersession and downstream invalidation cascade.
5. Ledger transaction edit invalidation.
6. Workspace locking immutability.
7. Path and PII masking in error messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.tax import (
    DocumentIntegrityError,
    EvidenceStatus,
    ExtractedField,
    Jurisdiction,
    ReconciliationStatus,
    TaxWorkspaceService,
    WorkspaceLockedError,
    mask_pii,
    sanitize_path,
)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate tax document storage into a temporary directory."""
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def tax_service(session: Session, engine) -> TaxWorkspaceService:
    init_db(engine)
    return TaxWorkspaceService(session)


def _register(tax_service: TaxWorkspaceService, workspace_id: str, name: str, rev: str = "1"):  # type: ignore[no-untyped-def]
    """Register a synthetic source document so evidence has a real parent.

    Evidence must reference a document in the same workspace, so tests that
    propose fields need an actual registered document rather than a made-up id.
    """
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    ).replace(b"%PDF-1.4", b"%PDF-1.4 rev" + rev.encode())
    return tax_service.register_document(workspace_id=workspace_id, file_name=name, source=pdf)


def test_workspace_creation_and_year_isolation(
    tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Workspaces are strictly isolated by tax year and jurisdiction."""
    ws_2025 = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    ws_2024 = tax_service.get_or_create_workspace(2024, Jurisdiction.FEDERAL)

    assert ws_2025.id != ws_2024.id
    assert ws_2025.tax_year == 2025
    assert ws_2024.tax_year == 2024

    # Idempotent retrieval for same year and jurisdiction
    ws_2025_again = tax_service.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    assert ws_2025_again.id == ws_2025.id

    # Propose field in 2025
    doc_2025 = _register(tax_service, ws_2025.id, "synthetic_2025.pdf")
    field_2025 = tax_service.propose_field(
        workspace_id=ws_2025.id,
        document_id=doc_2025.id,
        field_name="box1a_dividends",
        raw_text="$500.00",
        amount_cents=50000,
    )
    tax_service.verify_field(field_2025.id, verified_by="auditor@local")

    # 2025 has approved totals, 2024 must remain zero
    assert tax_service.compute_verified_totals(ws_2025.id) == 50000
    assert tax_service.compute_verified_totals(ws_2024.id) == 0


def test_document_registration_and_integrity_check(
    tax_service: TaxWorkspaceService,
    tax_env: Path,
) -> None:
    """Source documents are saved immutably with SHA-256 integrity verification."""
    ws = tax_service.get_or_create_workspace(2025)

    doc_bytes = b"%PDF-1.4 1 0 obj << /Type /Catalog >> endobj trailer << >> %%EOF"
    doc = tax_service.register_document(
        workspace_id=ws.id,
        file_name="brokerage_1099.pdf",
        source=doc_bytes,
        doc_type="1099_CONSOLIDATED",
    )

    assert doc.workspace_id == ws.id
    assert Path(doc.storage_path).exists()
    assert doc.sha256_hash != ""

    # Re-verify document on disk
    stored_path = tax_service.verify_document_on_disk(doc.id)
    assert stored_path == Path(doc.storage_path)

    # Re-uploading identical bytes returns existing document (no duplicate records)
    duplicate_doc = tax_service.register_document(
        workspace_id=ws.id,
        file_name="brokerage_1099_copy.pdf",
        source=doc_bytes,
        doc_type="1099_CONSOLIDATED",
    )
    assert duplicate_doc.id == doc.id

    # Tampering test: modify stored file permissions and content
    Path(doc.storage_path).chmod(0o600)
    Path(doc.storage_path).write_bytes(b"tampered content")

    with pytest.raises(DocumentIntegrityError, match="Integrity check failed"):
        tax_service.verify_document_on_disk(doc.id)


def test_approval_invariant_strictness(
    tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Unverified, rejected, and superseded values NEVER enter approved totals."""
    ws = tax_service.get_or_create_workspace(2025)
    doc = _register(tax_service, ws.id, "synthetic.pdf")

    # 1. Propose three fields
    f1 = tax_service.propose_field(ws.id, doc.id, "interest_income", "$100.00", 10000)
    f2 = tax_service.propose_field(ws.id, doc.id, "dividend_income", "$250.00", 25000)
    f3 = tax_service.propose_field(ws.id, doc.id, "tax_exempt_interest", "$50.00", 5000)

    # When all are PROPOSED, verified total must be 0
    assert tax_service.compute_verified_totals(ws.id) == 0

    # 2. Verify f1
    tax_service.verify_field(f1.id, verified_by="auditor_jane", notes="Matches Box 1")
    assert tax_service.compute_verified_totals(ws.id) == 10000

    # 3. Reject f2
    tax_service.reject_field(f2.id, rejected_by="auditor_jane", notes="Misread OCR")
    # Rejected row is preserved in DB for audit trail
    f2_db = tax_service.session.get(ExtractedField, f2.id)
    assert f2_db is not None
    assert f2_db.status == EvidenceStatus.REJECTED
    assert f2_db.notes == "Misread OCR"

    # Total must strictly include only f1
    assert tax_service.compute_verified_totals(ws.id) == 10000

    # 4. Verify f3
    tax_service.verify_field(f3.id, verified_by="auditor_jane")
    assert tax_service.compute_verified_totals(ws.id) == 15000


def test_correction_supersession_invalidates_downstream(
    tax_service: TaxWorkspaceService,
    tax_env: Path,
) -> None:
    """Registering a corrected document marks prior fields SUPERSEDED and downstreams STALE."""
    ws = tax_service.get_or_create_workspace(2025)

    # Register original 1099
    doc_v1 = tax_service.register_document(
        workspace_id=ws.id,
        file_name="1099_v1.pdf",
        source=b"original-1099-v1-bytes",
        doc_type="1099_B",
    )

    # Propose and verify fields on doc_v1
    f_old = tax_service.propose_field(ws.id, doc_v1.id, "short_term_proceeds", "$1,000.00", 100000)
    tax_service.verify_field(f_old.id, verified_by="reviewer_bob")
    assert tax_service.compute_verified_totals(ws.id) == 100000

    # Create reconciliation item and evidence packet
    rec_item = tax_service.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Form 8949",
        field_key="proceeds",
        source_working_paper_cents=100000,
        taxact_entered_cents=100000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
    )
    packet = tax_service.create_evidence_packet(ws.id, is_draft=False)

    assert rec_item.status == ReconciliationStatus.MATCHED
    assert packet.is_stale is False

    # Ingest CORRECTED 1099
    doc_v2 = tax_service.register_document(
        workspace_id=ws.id,
        file_name="1099_v2_corrected.pdf",
        source=b"corrected-1099-v2-bytes",
        doc_type="1099_B",
        is_corrected=True,
        supersedes_doc_id=doc_v1.id,
    )
    assert doc_v2.is_corrected is True
    assert doc_v2.supersedes_doc_id == doc_v1.id

    # Prior field must be SUPERSEDED and excluded from approved totals
    tax_service.session.refresh(f_old)
    assert f_old.status == EvidenceStatus.SUPERSEDED
    assert tax_service.compute_verified_totals(ws.id) == 0

    # Downstream reconciliation and evidence packet must now be STALE
    tax_service.session.refresh(rec_item)
    tax_service.session.refresh(packet)
    assert rec_item.status == ReconciliationStatus.STALE
    assert packet.is_stale is True


def test_ledger_transaction_edit_invalidation(tax_service: TaxWorkspaceService) -> None:
    """Editing a consumed ledger transaction marks affected reconciliation items STALE."""
    ws = tax_service.get_or_create_workspace(2025)

    rec = tax_service.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Schedule E",
        field_key="cleaning_repairs",
        source_working_paper_cents=75000,
        taxact_entered_cents=75000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
        consumed_transaction_ids=[42, 99],
    )
    packet = tax_service.create_evidence_packet(ws.id, is_draft=False)

    # Invalidate transaction 42
    invalidated = tax_service.invalidate_for_transaction_edit(42)
    assert invalidated == 1

    tax_service.session.refresh(rec)
    tax_service.session.refresh(packet)
    assert rec.status == ReconciliationStatus.STALE
    assert packet.is_stale is True


def test_rental_property_and_depreciation(tax_service: TaxWorkspaceService) -> None:
    """Rental property and expense records compute approved totals accurately."""
    ws = tax_service.get_or_create_workspace(2025)

    prop = tax_service.create_rental_property(
        workspace_id=ws.id,
        street_address="456 Elm St",
        city="San Francisco",
        state="CA",
        zip_code="94103",
        date_placed_in_service="2020-03-01",
    )

    exp1 = tax_service.record_rental_expense(
        workspace_id=ws.id,
        property_id=prop.id,
        category="Advertising",
        amount_cents=12000,
        raw_text="$120.00",
    )
    exp2 = tax_service.record_rental_expense(
        workspace_id=ws.id,
        property_id=prop.id,
        category="Cleaning",
        amount_cents=35000,
        raw_text="$350.00",
    )
    assert exp2.id.startswith("exp-")

    # Initially proposed: approved rental expenses should be empty
    assert tax_service.compute_verified_rental_expenses(ws.id) == {}

    # Verify only exp1
    tax_service.verify_rental_expense(exp1.id)
    summary = tax_service.compute_verified_rental_expenses(ws.id)
    assert summary == {"Advertising": 12000}


def test_investment_sale_records(
    tax_service: TaxWorkspaceService, tax_env: Path
) -> None:
    """Investment sales correctly separate short-term from long-term verified gains."""
    ws = tax_service.get_or_create_workspace(2025)
    brokerage_doc = _register(tax_service, ws.id, "brokerage.pdf")

    sale_st = tax_service.record_investment_sale(
        workspace_id=ws.id,
        source_document_id=brokerage_doc.id,
        security_description="AAPL",
        date_sold="2025-06-15",
        proceeds_cents=500000,
        cost_basis_cents=450000,
        gain_loss_cents=50000,
        term="SHORT_TERM",
        raw_text="$5,000.00",
    )
    sale_lt = tax_service.record_investment_sale(
        workspace_id=ws.id,
        source_document_id=brokerage_doc.id,
        security_description="MSFT",
        date_sold="2025-08-20",
        proceeds_cents=1200000,
        cost_basis_cents=800000,
        gain_loss_cents=400000,
        term="LONG_TERM",
        raw_text="$12,000.00",
    )

    # Record a sale with missing/undetermined basis (noncovered security)
    sale_missing = tax_service.record_investment_sale(
        workspace_id=ws.id,
        source_document_id=brokerage_doc.id,
        security_description="CRYPTO_TOKEN",
        date_sold="2025-09-10",
        proceeds_cents=300000,
        cost_basis_cents=None,
        gain_loss_cents=None,
        term="SHORT_TERM",
        basis_reported_to_irs=False,
    )
    # Verify all sales
    tax_service.verify_investment_sale(sale_st.id)
    tax_service.verify_investment_sale(sale_lt.id)
    tax_service.verify_investment_sale(sale_missing.id)

    gains = tax_service.compute_verified_investment_gains(ws.id)
    assert gains["short_term_proceeds_cents"] == 800000
    assert gains["short_term_basis_cents"] == 450000
    assert gains["short_term_gain_loss_cents"] == 50000
    assert gains["long_term_proceeds_cents"] == 1200000
    assert gains["long_term_gain_loss_cents"] == 400000
    assert gains["missing_basis_count"] == 1


def test_workspace_locking(tax_service: TaxWorkspaceService) -> None:
    """Locked workspace rejects all mutations."""
    ws = tax_service.get_or_create_workspace(2025)
    tax_service.lock_workspace(ws.id)

    with pytest.raises(WorkspaceLockedError, match="LOCKED and immutable"):
        tax_service.propose_field(ws.id, "doc-x", "dividend", "$50.00", 5000)

    with pytest.raises(WorkspaceLockedError, match="LOCKED and immutable"):
        tax_service.create_rental_property(ws.id, "123 Main", "Oakland", "CA", "94601")


def test_pii_and_path_masking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sensitive paths and SSNs are masked from error messages."""
    tax_dir = tmp_path / "private_tax"
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))

    raw_msg = f"Failed opening {tax_dir}/2025/doc-123.pdf for SSN 123-45-6789"
    sanitized = mask_pii(sanitize_path(raw_msg))
    assert "123-45-6789" not in sanitized
    assert "***-**-****" in sanitized
    assert str(tax_dir) not in sanitized
    assert "[TAX_STORAGE]" in sanitized
