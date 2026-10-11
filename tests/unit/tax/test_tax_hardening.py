"""TAX-04 hardening regression tests.

Each test here reproduces a defect found in review of the tax backend, on
2026-09-28, and pins the corrected behaviour:

1. Evidence crossing tax years - a field in one workspace could reference
   another workspace's document, contribute to that workspace's verified
   totals, and be mutated when that document was corrected.
2. Superseded evidence could be re-verified, putting a replaced amount back
   into totals.
3. A real ledger deletion never invalidated the tax comparisons that consumed
   the deleted transaction, because the ledger mutation path did not call the
   invalidation helper. The pre-existing test called the helper directly and
   so missed the integration gap.
4. A non-draft evidence packet could be created while questions were still
   open - the "approved" designation had no readiness gate.
5. Residency and filing status defaulted to unconfirmed values, which
   presented an unconfirmed fact as a known one.

These are written to fail against the pre-fix service and pass after it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select
from tests.factories import make_account

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Transaction
from local_ledger.tax import (
    DocumentNotFoundError,
    EvidenceStatus,
    ExtractedField,
    FilingStatus,
    Jurisdiction,
    ReconciliationItem,
    ReconciliationStatus,
    ResidencyStatus,
    TaxWorkspaceService,
)
from local_ledger.tax.scope import ReviewScopeMode, WorkspaceScopeService
from local_ledger.tax.service import (
    EntityNotFoundError,
    InvalidStateTransitionError,
    PacketReadinessError,
    WorkspaceScopeError,
)

# A minimal but structurally valid single-page PDF with a text layer.
_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 12 Tf 20 100 Td (SYNTHETIC 1099-B) Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


def _pdf(revision: bytes = b"1") -> bytes:
    """Same document, differing bytes - a correction must actually differ."""
    return _PDF.replace(b"SYNTHETIC 1099-B", b"SYNTHETIC 1099-B rev" + revision)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate tax document storage into a temporary directory."""
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def svc(session: Session, engine) -> TaxWorkspaceService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return TaxWorkspaceService(session)


def _doc(svc: TaxWorkspaceService, workspace_id: str, name: str = "1099b.pdf", rev: bytes = b"1", **kw):  # type: ignore[no-untyped-def]
    return svc.register_document(workspace_id=workspace_id, file_name=name, source=_pdf(rev), **kw)


# --- 1. year and workspace isolation ---


def test_field_cannot_reference_a_document_from_another_workspace(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """A 2024 workspace must not be able to hang evidence off a 2025 document."""
    ws_2024 = svc.get_or_create_workspace(2024, Jurisdiction.FEDERAL)
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc_2025 = _doc(svc, ws_2025.id)

    with pytest.raises(WorkspaceScopeError):
        svc.propose_field(
            workspace_id=ws_2024.id,
            document_id=doc_2025.id,
            field_name="proceeds",
            raw_text="$100.00",
            amount_cents=10000,
        )

    assert svc.compute_verified_totals(ws_2024.id) == 0


def test_field_cannot_reference_a_nonexistent_document(svc: TaxWorkspaceService) -> None:
    """An invented document id is not evidence."""
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    with pytest.raises(DocumentNotFoundError):
        svc.propose_field(
            workspace_id=ws_2025.id,
            document_id="doc-does-not-exist",
            field_name="proceeds",
            raw_text="$100.00",
            amount_cents=10000,
        )


def test_document_from_another_year_stays_out_of_the_workspace(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """Jurisdiction siblings share a year but never a document."""
    fed = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    ca = svc.get_or_create_workspace(2025, Jurisdiction.CALIFORNIA)
    doc_fed = _doc(svc, fed.id)

    with pytest.raises(WorkspaceScopeError):
        svc.propose_field(
            workspace_id=ca.id,
            document_id=doc_fed.id,
            field_name="proceeds",
            raw_text="$100.00",
            amount_cents=10000,
        )


def test_correction_does_not_supersede_another_workspaces_field(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """Supersession is scoped to the corrected document's own workspace."""
    ws_2024 = svc.get_or_create_workspace(2024, Jurisdiction.FEDERAL)
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc_2025 = _doc(svc, ws_2025.id)
    doc_2024 = _doc(svc, ws_2024.id, name="rent_2024.pdf", rev=b"rent")

    field_2024 = svc.propose_field(
        workspace_id=ws_2024.id,
        document_id=doc_2024.id,
        field_name="rent",
        raw_text="$1,000.00",
        amount_cents=100000,
    )
    svc.verify_field(field_2024.id, verified_by="auditor@local")

    # Correcting the *2025* document must not touch the 2024 field.
    _doc(
        svc,
        ws_2025.id,
        name="1099b_corrected.pdf",
        rev=b"2",
        is_corrected=True,
        supersedes_doc_id=doc_2025.id,
    )

    svc.session.refresh(field_2024)
    assert field_2024.status == EvidenceStatus.VERIFIED
    assert svc.compute_verified_totals(ws_2024.id) == 100000


def test_correction_cannot_supersede_a_document_in_another_workspace(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """A correction may only supersede a document in its own workspace."""
    ws_2024 = svc.get_or_create_workspace(2024, Jurisdiction.FEDERAL)
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc_2024 = _doc(svc, ws_2024.id, name="rent_2024.pdf")

    with pytest.raises(WorkspaceScopeError):
        _doc(
            svc,
            ws_2025.id,
            name="rogue_correction.pdf",
            rev=b"2",
            is_corrected=True,
            supersedes_doc_id=doc_2024.id,
        )


def test_field_cannot_be_proposed_from_a_superseded_document(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """Re-extracting from the replaced original must not resurrect it."""
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws_2025.id)
    _doc(svc, ws_2025.id, name="corrected.pdf", rev=b"2", is_corrected=True, supersedes_doc_id=doc.id)

    with pytest.raises(DocumentNotFoundError):
        svc.propose_field(
            workspace_id=ws_2025.id,
            document_id=doc.id,
            field_name="proceeds",
            raw_text="$100.00",
            amount_cents=10000,
        )


# --- 2. state transitions ---


def test_superseded_field_cannot_be_reverified(svc: TaxWorkspaceService, tax_env: Path) -> None:
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws_2025.id)
    field = svc.propose_field(
        workspace_id=ws_2025.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    _doc(svc, ws_2025.id, name="corrected.pdf", rev=b"2", is_corrected=True, supersedes_doc_id=doc.id)

    with pytest.raises(InvalidStateTransitionError):
        svc.verify_field(field.id, verified_by="auditor@local")

    svc.session.refresh(field)
    assert field.status == EvidenceStatus.SUPERSEDED
    assert svc.compute_verified_totals(ws_2025.id) == 0


def test_rejected_field_cannot_be_reverified(svc: TaxWorkspaceService, tax_env: Path) -> None:
    """A rejection is a decision; re-proposing is the way back, not re-verifying."""
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws_2025.id)
    field = svc.propose_field(
        workspace_id=ws_2025.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.reject_field(field.id, rejected_by="auditor@local", notes="wrong box")

    with pytest.raises(InvalidStateTransitionError):
        svc.verify_field(field.id, verified_by="auditor@local")

    svc.session.refresh(field)
    assert field.status == EvidenceStatus.REJECTED
    assert svc.compute_verified_totals(ws_2025.id) == 0


def test_already_verified_field_cannot_be_verified_again(svc: TaxWorkspaceService, tax_env: Path) -> None:
    """Re-verification is a no-op at best; it must not silently re-attribute."""
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws_2025.id)
    field = svc.propose_field(
        workspace_id=ws_2025.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="first@local")

    with pytest.raises(InvalidStateTransitionError):
        svc.verify_field(field.id, verified_by="second@local")

    svc.session.refresh(field)
    assert field.verified_by == "first@local"


def test_unknown_field_cannot_be_verified(svc: TaxWorkspaceService) -> None:
    with pytest.raises(EntityNotFoundError):
        svc.verify_field("fld-nope", verified_by="auditor@local")


# --- 3. ledger mutation integration ---


def test_deleting_a_ledger_transaction_invalidates_the_tax_comparison(
    session: Session, engine, svc: TaxWorkspaceService
) -> None:  # type: ignore[no-untyped-def]
    """The integration gap: the helper worked, nothing called it."""
    init_db(engine)
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    acc = make_account(session)
    session.commit()
    tx = Transaction(account_id=acc.id, date="2025-01-15", amount_cents=-25000)
    session.add(tx)
    session.commit()

    item = svc.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Schedule E",
        field_key="rental_repairs",
        source_working_paper_cents=25000,
        taxact_entered_cents=25000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
        consumed_transaction_ids=[tx.id],
    )
    assert item.status == ReconciliationStatus.MATCHED

    # The real mutation path, not the helper.
    LedgerService(session).delete_transaction(tx.id, actor="user")

    session.refresh(item)
    assert item.status == ReconciliationStatus.STALE, (
        "deleting a transaction that a tax comparison consumed must invalidate it"
    )


def test_deleting_an_unrelated_transaction_leaves_comparison_intact(
    session: Session, engine, svc: TaxWorkspaceService
) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    acc = make_account(session)
    session.commit()
    used = Transaction(account_id=acc.id, date="2025-01-15", amount_cents=-25000)
    other = Transaction(account_id=acc.id, date="2025-02-15", amount_cents=-1000)
    session.add(used)
    session.add(other)
    session.commit()

    item = svc.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Schedule E",
        field_key="rental_repairs",
        source_working_paper_cents=25000,
        taxact_entered_cents=25000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
        consumed_transaction_ids=[used.id],
    )

    LedgerService(session).delete_transaction(other.id, actor="user")

    session.refresh(item)
    assert item.status == ReconciliationStatus.MATCHED


def test_deleting_a_transfer_mirror_invalidates_both_ids(
    session: Session, engine, svc: TaxWorkspaceService
) -> None:  # type: ignore[no-untyped-def]
    """Deleting a transfer deletes two rows; both must invalidate what used them."""
    init_db(engine)
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    out_acc = make_account(session, name="Checking")
    in_acc = make_account(session, name="Savings")
    session.commit()
    ledger = LedgerService(session)
    out_tx, in_tx = ledger.create_transfer(
        from_account_id=out_acc.id,
        to_account_id=in_acc.id,
        amount_cents=500000,
        date="2025-03-31",
    )

    item = svc.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Schedule D",
        field_key="transfer_exclusion",
        source_working_paper_cents=500000,
        taxact_entered_cents=0,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
        consumed_transaction_ids=[in_tx.id],
    )

    ledger.delete_transaction(out_tx.id, actor="user")

    session.refresh(item)
    assert item.status == ReconciliationStatus.STALE


# --- 4. packet readiness ---


def test_non_draft_packet_is_refused_while_questions_are_open(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws.id)
    field = svc.propose_field(
        workspace_id=ws.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    svc.create_tax_question(
        workspace_id=ws.id,
        question_text="Lot B basis not reported on the 1099-B",
        context="box 2",
    )

    with pytest.raises(PacketReadinessError) as exc:
        svc.create_evidence_packet(ws.id, is_draft=False)
    assert "1 open question" in str(exc.value)

    # A draft is still allowed, and is still labelled a draft.
    draft = svc.create_evidence_packet(ws.id, is_draft=True)
    assert draft.is_draft is True


def test_non_draft_packet_is_allowed_once_questions_are_resolved(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws.id)
    field = svc.propose_field(
        workspace_id=ws.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    q = svc.create_tax_question(
        workspace_id=ws.id, question_text="Lot B basis missing", context="box 2"
    )
    svc.resolve_tax_question(q.id, resolution="Asked the broker; basis confirmed")

    WorkspaceScopeService(svc.session).configure(
        workspace_id=ws.id, mode=ReviewScopeMode.DOCUMENTS_ONLY, properties_confirmed=True,
    )
    svc.record_reconciliation_item(
        workspace_id=ws.id, tax_section="Form 8949", field_key="proceeds",
        source_working_paper_cents=50000, taxact_entered_cents=50000,
        delta_cents=0, status=ReconciliationStatus.MATCHED,
    )
    packet = svc.create_evidence_packet(ws.id, is_draft=False)
    assert packet.is_draft is False
    assert packet.is_stale is False


def test_stale_evidence_cannot_be_promoted_to_a_final_packet(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    """A correction after approval must force a new review, not a stale 'approved'."""
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws.id)
    field = svc.propose_field(
        workspace_id=ws.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    WorkspaceScopeService(svc.session).configure(
        workspace_id=ws.id, mode=ReviewScopeMode.DOCUMENTS_ONLY, properties_confirmed=True,
    )
    svc.record_reconciliation_item(
        workspace_id=ws.id, tax_section="Form 8949", field_key="proceeds",
        source_working_paper_cents=50000, taxact_entered_cents=50000,
        delta_cents=0, status=ReconciliationStatus.MATCHED,
    )
    packet = svc.create_evidence_packet(ws.id, is_draft=False)
    assert packet.is_stale is False

    _doc(svc, ws.id, name="corrected.pdf", rev=b"2", is_corrected=True, supersedes_doc_id=doc.id)

    svc.session.refresh(packet)
    assert packet.is_stale is True


# --- 5. unconfirmed facts stay unconfirmed ---


def test_workspace_does_not_assert_residency_or_filing_status(svc: TaxWorkspaceService) -> None:
    """Neither fact has been confirmed by the user, so neither may be defaulted."""
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    assert ws.residency_status == ResidencyStatus.UNKNOWN
    assert ws.filing_status == FilingStatus.UNKNOWN


def test_confirmed_facts_can_still_be_recorded(svc: TaxWorkspaceService) -> None:
    ws = svc.get_or_create_workspace(
        2025,
        Jurisdiction.FEDERAL,
        residency_status=ResidencyStatus.FULL_YEAR,
        filing_status=FilingStatus.MARRIED_FILING_JOINTLY,
    )
    assert ws.residency_status == ResidencyStatus.FULL_YEAR
    assert ws.filing_status == FilingStatus.MARRIED_FILING_JOINTLY


# --- invariants that must survive all of the above ---


def test_hardening_does_not_leak_across_years_end_to_end(
    svc: TaxWorkspaceService, tax_env: Path
) -> None:
    ws_2024 = svc.get_or_create_workspace(2024, Jurisdiction.FEDERAL)
    ws_2025 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc_2024 = _doc(svc, ws_2024.id, name="rent_2024.pdf")
    doc_2025 = _doc(svc, ws_2025.id, name="1099b_2025.pdf")

    f24 = svc.propose_field(
        workspace_id=ws_2024.id,
        document_id=doc_2024.id,
        field_name="rent",
        raw_text="$1,000.00",
        amount_cents=100000,
    )
    f25 = svc.propose_field(
        workspace_id=ws_2025.id,
        document_id=doc_2025.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(f24.id, verified_by="auditor@local")
    svc.verify_field(f25.id, verified_by="auditor@local")

    _doc(svc, ws_2025.id, name="1099b_2025_corrected.pdf", rev=b"2", is_corrected=True, supersedes_doc_id=doc_2025.id)

    assert svc.compute_verified_totals(ws_2024.id) == 100000
    assert svc.compute_verified_totals(ws_2025.id) == 0

    rows = svc.session.exec(select(ExtractedField)).all()
    by_id = {r.id: r for r in rows}
    assert by_id[f24.id].status == EvidenceStatus.VERIFIED
    assert by_id[f25.id].status == EvidenceStatus.SUPERSEDED


def test_reconciliation_items_survive_the_cascade(svc: TaxWorkspaceService, tax_env: Path) -> None:
    """Invalidation marks work stale; it never deletes it."""
    ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = _doc(svc, ws.id)
    item = svc.record_reconciliation_item(
        workspace_id=ws.id,
        tax_section="Schedule D",
        field_key="proceeds",
        source_working_paper_cents=50000,
        taxact_entered_cents=50000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
    )
    field = svc.propose_field(
        workspace_id=ws.id,
        document_id=doc.id,
        field_name="proceeds",
        raw_text="$500.00",
        amount_cents=50000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")

    svc.session.refresh(item)
    assert item.status == ReconciliationStatus.STALE
    assert svc.session.exec(select(ReconciliationItem)).all() == [item]
