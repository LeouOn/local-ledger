"""Tax Workspace and Evidence Lifecycle Domain Service (TAX-04).

Enforces invariants:
1. Approval invariant: Only VERIFIED evidence fields enter approved totals.
   PROPOSED, REJECTED, and SUPERSEDED fields are strictly excluded.
2. Year isolation: All documents, fields, properties, questions, and reconciliations
   are partitioned by tax_year and workspace_id.
3. Corrections and supersession: Ingesting a corrected document marks prior fields
   as SUPERSEDED and marks downstream reconciliation items and evidence packets as STALE.
4. Ledger invalidation: Modifying or deleting a consumed ledger transaction invalidates
   affected reconciliation items and evidence packets.
5. Error masking: Sensitive file paths, usernames, and SSNs never leak into error messages.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from local_ledger.tax.models import (
    DocumentType,
    EvidencePacket,
    EvidenceStatus,
    ExtractedField,
    FilingStatus,
    InvestmentSaleRecord,
    Jurisdiction,
    ReconciliationItem,
    ReconciliationStatus,
    RentalExpenseRecord,
    ResidencyStatus,
    TaxDocument,
    TaxProperty,
    TaxQuestion,
    TaxWorkspace,
    WorkspaceStatus,
)
from local_ledger.tax.storage import (
    DocumentNotFoundError,
    compute_sha256,
    mask_pii,
    sanitize_path,
    store_document_file,
    verify_stored_document,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaxServiceError(Exception):
    """Base exception for tax domain operations with sanitized messages."""

    def __init__(self, message: str) -> None:
        super().__init__(mask_pii(sanitize_path(message)))


class WorkspaceLockedError(TaxServiceError):
    """Raised when an attempt is made to modify a locked tax workspace."""


class WorkspaceNotFoundError(TaxServiceError):
    """Raised when the specified tax workspace does not exist."""


class EntityNotFoundError(TaxServiceError):
    """Raised when an expected tax entity is not found."""


class DuplicateDocumentError(TaxServiceError):
    """Raised when an identical document hash is uploaded for the same workspace."""


class TaxWorkspaceService:
    """Service governing annual tax workspaces, evidence lifecycle, and audit integrity."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------------------
    # Workspace Lifecycle
    # -------------------------------------------------------------------------

    def get_or_create_workspace(
        self,
        tax_year: int,
        jurisdiction: str = Jurisdiction.FEDERAL,
        residency_status: str = ResidencyStatus.FULL_YEAR,
        filing_status: str = FilingStatus.SINGLE,
    ) -> TaxWorkspace:
        """Fetch existing workspace for year + jurisdiction, or create a new draft."""
        stmt = select(TaxWorkspace).where(
            TaxWorkspace.tax_year == tax_year,
            TaxWorkspace.jurisdiction == jurisdiction,
        )
        workspace = self.session.exec(stmt).first()
        if workspace is not None:
            return workspace

        workspace = TaxWorkspace(
            tax_year=tax_year,
            jurisdiction=jurisdiction,
            residency_status=residency_status,
            filing_status=filing_status,
            status=WorkspaceStatus.DRAFT,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        self.session.add(workspace)
        self.session.commit()
        self.session.refresh(workspace)
        return workspace

    def get_workspace(self, workspace_id: str) -> TaxWorkspace | None:
        """Retrieve a workspace by ID."""
        return self.session.get(TaxWorkspace, workspace_id)

    def _require_workspace(self, workspace_id: str) -> TaxWorkspace:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")
        return ws

    def _check_not_locked(self, workspace: TaxWorkspace) -> None:
        if workspace.status == WorkspaceStatus.LOCKED:
            raise WorkspaceLockedError(
                f"Workspace for year {workspace.tax_year} ({workspace.jurisdiction}) is LOCKED and immutable"
            )

    def lock_workspace(self, workspace_id: str) -> TaxWorkspace:
        """Lock workspace against any further edits."""
        workspace = self._require_workspace(workspace_id)
        workspace.status = WorkspaceStatus.LOCKED
        workspace.updated_at = _utcnow()
        self.session.add(workspace)
        self.session.commit()
        self.session.refresh(workspace)
        return workspace

    # -------------------------------------------------------------------------
    # Document Ingestion & Supersession
    # -------------------------------------------------------------------------

    def register_document(
        self,
        workspace_id: str,
        file_name: str,
        source: Path | bytes,
        doc_type: str = DocumentType.OTHER_EVIDENCE,
        is_corrected: bool = False,
        supersedes_doc_id: str | None = None,
    ) -> TaxDocument:
        """Register a source tax document, storing it immutably in private storage.

        If `is_corrected` is True and `supersedes_doc_id` is supplied:
        - Marks fields linked to the superseded document as SUPERSEDED.
        - Marks downstream reconciliation items and evidence packets as STALE.
        """
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        # Check for pre-existing document with same SHA-256 in this workspace
        if isinstance(source, (bytes, bytearray)):
            incoming_hash = compute_sha256(bytes(source))
        else:
            p = Path(source)
            if not p.is_file():
                raise DocumentNotFoundError(f"File not found: {sanitize_path(p)}")
            incoming_hash = compute_sha256(p)

        existing_stmt = select(TaxDocument).where(
            TaxDocument.workspace_id == workspace_id,
            TaxDocument.sha256_hash == incoming_hash,
        )
        existing = self.session.exec(existing_stmt).first()
        if existing is not None:
            # Per contract §11: duplicate bytes does not create a second document
            return existing

        # Generate new document ID
        new_doc = TaxDocument(
            workspace_id=workspace_id,
            file_name=file_name,
            sha256_hash=incoming_hash,
            doc_type=doc_type,
            is_corrected=is_corrected,
            supersedes_doc_id=supersedes_doc_id,
            ingested_at=_utcnow(),
        )

        # Save to private storage outside repository
        storage_path, sha256_verified, page_count, scan_state = store_document_file(
            source=source,
            file_name=file_name,
            tax_year=workspace.tax_year,
            doc_id=new_doc.id,
        )
        new_doc.storage_path = storage_path
        new_doc.sha256_hash = sha256_verified
        new_doc.page_count = page_count
        new_doc.scan_state = scan_state

        self.session.add(new_doc)

        # Handle supersession invalidation cascade
        if is_corrected and supersedes_doc_id:
            self._handle_supersession(workspace_id, supersedes_doc_id)

        workspace.updated_at = _utcnow()
        self.session.add(workspace)
        self.session.commit()
        self.session.refresh(new_doc)
        return new_doc

    def _handle_supersession(self, workspace_id: str, old_doc_id: str) -> None:
        """Mark old document's fields as SUPERSEDED and mark downstream items STALE."""
        # 1. Supersede ExtractedField rows
        fields_stmt = select(ExtractedField).where(
            ExtractedField.document_id == old_doc_id,
            ExtractedField.status != EvidenceStatus.SUPERSEDED,
        )
        for field in self.session.exec(fields_stmt).all():
            field.status = EvidenceStatus.SUPERSEDED
            self.session.add(field)

        # 2. Supersede RentalExpenseRecord rows linked to old document
        rentals_stmt = select(RentalExpenseRecord).where(
            RentalExpenseRecord.source_document_id == old_doc_id,
            RentalExpenseRecord.status != EvidenceStatus.SUPERSEDED,
        )
        for exp in self.session.exec(rentals_stmt).all():
            exp.status = EvidenceStatus.SUPERSEDED
            self.session.add(exp)

        # 3. Supersede InvestmentSaleRecord rows linked to old document
        sales_stmt = select(InvestmentSaleRecord).where(
            InvestmentSaleRecord.source_document_id == old_doc_id,
            InvestmentSaleRecord.status != EvidenceStatus.SUPERSEDED,
        )
        for sale in self.session.exec(sales_stmt).all():
            sale.status = EvidenceStatus.SUPERSEDED
            self.session.add(sale)

        # 4. Invalidate downstream reconciliation items and packets
        self._invalidate_downstream(workspace_id)

    def _invalidate_downstream(self, workspace_id: str) -> None:
        """Mark all reconciliation items and evidence packets in workspace as STALE."""
        recs_stmt = select(ReconciliationItem).where(
            ReconciliationItem.workspace_id == workspace_id,
            ReconciliationItem.status != ReconciliationStatus.STALE,
        )
        for rec in self.session.exec(recs_stmt).all():
            rec.status = ReconciliationStatus.STALE
            self.session.add(rec)

        packets_stmt = select(EvidencePacket).where(
            EvidencePacket.workspace_id == workspace_id,
            EvidencePacket.is_stale == False,  # noqa: E712
        )
        for pkt in self.session.exec(packets_stmt).all():
            pkt.is_stale = True
            self.session.add(pkt)

    def get_document(self, document_id: str) -> TaxDocument | None:
        """Retrieve a document by ID."""
        return self.session.get(TaxDocument, document_id)

    def verify_document_on_disk(self, document_id: str) -> Path:
        """Check stored file integrity against its recorded SHA-256 hash."""
        doc = self.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(f"Document {document_id} not found in database")
        return verify_stored_document(doc.storage_path, doc.sha256_hash)

    # -------------------------------------------------------------------------
    # Extracted Field Lifecycle (PROPOSED -> VERIFIED / REJECTED / SUPERSEDED)
    # -------------------------------------------------------------------------

    def propose_field(
        self,
        workspace_id: str,
        document_id: str,
        field_name: str,
        raw_text: str,
        amount_cents: int,
        page_number: int = 1,
        box_or_line: str = "",
        snippet_text: str = "",
        bounding_box: tuple[float, float, float, float] | None = None,
        model_provenance: dict[str, Any] | None = None,
    ) -> ExtractedField:
        """Record an unverified extraction proposal from local model or OCR."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        bbox_json = json.dumps(bounding_box) if bounding_box else None
        prov_json = json.dumps(model_provenance) if model_provenance else None

        field = ExtractedField(
            workspace_id=workspace_id,
            document_id=document_id,
            field_name=field_name,
            raw_text=raw_text,
            amount_cents=amount_cents,
            page_number=page_number,
            box_or_line=box_or_line,
            snippet_text=snippet_text,
            bounding_box_json=bbox_json,
            model_provenance_json=prov_json,
            status=EvidenceStatus.PROPOSED,
        )
        self.session.add(field)
        self.session.commit()
        self.session.refresh(field)
        return field

    def verify_field(
        self,
        field_id: str,
        verified_by: str,
        notes: str | None = None,
    ) -> ExtractedField:
        """Confirm an extraction proposal, transitioning it to VERIFIED status.

        Marks downstream reconciliation lines and evidence packets STALE.
        """
        field = self.session.get(ExtractedField, field_id)
        if field is None:
            raise EntityNotFoundError(f"ExtractedField {field_id} not found")

        workspace = self._require_workspace(field.workspace_id)
        self._check_not_locked(workspace)

        field.status = EvidenceStatus.VERIFIED
        field.verified_by = verified_by
        field.verified_at = _utcnow()
        if notes:
            field.notes = notes

        self.session.add(field)
        self._invalidate_downstream(field.workspace_id)
        workspace.updated_at = _utcnow()
        self.session.add(workspace)
        self.session.commit()
        self.session.refresh(field)
        return field

    def reject_field(
        self,
        field_id: str,
        rejected_by: str,
        notes: str,
    ) -> ExtractedField:
        """Reject an extraction proposal. Retained for audit trail; never enters totals."""
        field = self.session.get(ExtractedField, field_id)
        if field is None:
            raise EntityNotFoundError(f"ExtractedField {field_id} not found")

        workspace = self._require_workspace(field.workspace_id)
        self._check_not_locked(workspace)

        field.status = EvidenceStatus.REJECTED
        field.verified_by = rejected_by
        field.verified_at = _utcnow()
        field.notes = notes

        self.session.add(field)
        self._invalidate_downstream(field.workspace_id)
        workspace.updated_at = _utcnow()
        self.session.add(workspace)
        self.session.commit()
        self.session.refresh(field)
        return field

    # -------------------------------------------------------------------------
    # Approval Invariant Totals Computation
    # -------------------------------------------------------------------------

    def compute_verified_totals(
        self,
        workspace_id: str,
        field_name: str | None = None,
    ) -> int:
        """Sum verified amounts for a workspace.

        Strict Invariant: ONLY records with status == VERIFIED contribute to approved totals.
        PROPOSED, REJECTED, and SUPERSEDED records are strictly excluded.
        """
        stmt = select(func.coalesce(func.sum(ExtractedField.amount_cents), 0)).where(
            ExtractedField.workspace_id == workspace_id,
            ExtractedField.status == EvidenceStatus.VERIFIED,
        )
        if field_name:
            stmt = stmt.where(ExtractedField.field_name == field_name)

        total = self.session.exec(stmt).one()
        return int(total)

    # -------------------------------------------------------------------------
    # Rental Property & Expense Records
    # -------------------------------------------------------------------------

    def create_rental_property(
        self,
        workspace_id: str,
        street_address: str,
        city: str,
        state: str,
        zip_code: str,
        property_type: str = "Single Family",
        ownership_basis_points: int = 10000,
        days_rented: int = 365,
        days_personal_use: int = 0,
        date_placed_in_service: str = "",
    ) -> TaxProperty:
        """Add a rental property under a tax workspace."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        prop = TaxProperty(
            workspace_id=workspace_id,
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
            property_type=property_type,
            ownership_basis_points=ownership_basis_points,
            days_rented=days_rented,
            days_personal_use=days_personal_use,
            date_placed_in_service=date_placed_in_service,
        )
        self.session.add(prop)
        self.session.commit()
        self.session.refresh(prop)
        return prop

    def record_rental_expense(
        self,
        workspace_id: str,
        property_id: str,
        category: str,
        amount_cents: int,
        raw_text: str = "",
        source_document_id: str | None = None,
        source_transaction_id: int | None = None,
        page_number: int | None = None,
        box_or_line: str | None = None,
        treatment_authority: str = "IRC § 162 / IRS Pub 527",
        is_capital_improvement_suspect: bool = False,
        status: str = EvidenceStatus.PROPOSED,
    ) -> RentalExpenseRecord:
        """Add an operating expense line item for a rental property."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        record = RentalExpenseRecord(
            workspace_id=workspace_id,
            property_id=property_id,
            category=category,
            amount_cents=amount_cents,
            raw_text=raw_text,
            source_document_id=source_document_id,
            source_transaction_id=source_transaction_id,
            page_number=page_number,
            box_or_line=box_or_line,
            treatment_authority=treatment_authority,
            is_capital_improvement_suspect=is_capital_improvement_suspect,
            status=status,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    def verify_rental_expense(self, expense_id: str) -> RentalExpenseRecord:
        """Mark a rental expense as VERIFIED."""
        record = self.session.get(RentalExpenseRecord, expense_id)
        if record is None:
            raise EntityNotFoundError(f"RentalExpenseRecord {expense_id} not found")

        workspace = self._require_workspace(record.workspace_id)
        self._check_not_locked(workspace)

        record.status = EvidenceStatus.VERIFIED
        self.session.add(record)
        self._invalidate_downstream(record.workspace_id)
        self.session.commit()
        self.session.refresh(record)
        return record

    def compute_verified_rental_expenses(
        self,
        workspace_id: str,
        property_id: str | None = None,
    ) -> dict[str, int]:
        """Compute approved expense totals by category for a property or workspace.

        Strict Invariant: ONLY VERIFIED expenses are included.
        """
        stmt = (
            select(
                RentalExpenseRecord.category,
                func.sum(RentalExpenseRecord.amount_cents),
            )
            .where(
                RentalExpenseRecord.workspace_id == workspace_id,
                RentalExpenseRecord.status == EvidenceStatus.VERIFIED,
            )
            .group_by(RentalExpenseRecord.category)
        )
        if property_id:
            stmt = stmt.where(RentalExpenseRecord.property_id == property_id)

        rows = self.session.exec(stmt).all()
        return {cat: int(total) for cat, total in rows}

    # -------------------------------------------------------------------------
    # Investment Dispositions (Form 8949 / Schedule D)
    # -------------------------------------------------------------------------

    def record_investment_sale(
        self,
        workspace_id: str,
        source_document_id: str,
        security_description: str,
        date_sold: str,
        proceeds_cents: int,
        cost_basis_cents: int,
        gain_loss_cents: int,
        cusip_or_ticker: str | None = None,
        date_acquired: str | None = None,
        term: str = "SHORT_TERM",
        basis_reported_to_irs: bool = True,
        wash_sale_disallowed_cents: int = 0,
        fed_ca_difference_cents: int = 0,
        raw_text: str = "",
        status: str = EvidenceStatus.PROPOSED,
    ) -> InvestmentSaleRecord:
        """Record an investment disposition line item."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        sale = InvestmentSaleRecord(
            workspace_id=workspace_id,
            source_document_id=source_document_id,
            security_description=security_description,
            cusip_or_ticker=cusip_or_ticker,
            date_acquired=date_acquired,
            date_sold=date_sold,
            proceeds_cents=proceeds_cents,
            cost_basis_cents=cost_basis_cents,
            gain_loss_cents=gain_loss_cents,
            term=term,
            basis_reported_to_irs=basis_reported_to_irs,
            wash_sale_disallowed_cents=wash_sale_disallowed_cents,
            fed_ca_difference_cents=fed_ca_difference_cents,
            raw_text=raw_text,
            status=status,
        )
        self.session.add(sale)
        self.session.commit()
        self.session.refresh(sale)
        return sale

    def verify_investment_sale(self, sale_id: str) -> InvestmentSaleRecord:
        """Mark an investment sale record as VERIFIED."""
        sale = self.session.get(InvestmentSaleRecord, sale_id)
        if sale is None:
            raise EntityNotFoundError(f"InvestmentSaleRecord {sale_id} not found")

        workspace = self._require_workspace(sale.workspace_id)
        self._check_not_locked(workspace)

        sale.status = EvidenceStatus.VERIFIED
        self.session.add(sale)
        self._invalidate_downstream(sale.workspace_id)
        self.session.commit()
        self.session.refresh(sale)
        return sale

    def compute_verified_investment_gains(self, workspace_id: str) -> dict[str, int]:
        """Aggregate approved investment sales by holding term.

        Strict Invariant: ONLY VERIFIED sales are counted.
        """
        stmt = (
            select(
                InvestmentSaleRecord.term,
                func.sum(InvestmentSaleRecord.proceeds_cents),
                func.sum(InvestmentSaleRecord.cost_basis_cents),
                func.sum(InvestmentSaleRecord.gain_loss_cents),
                func.sum(InvestmentSaleRecord.wash_sale_disallowed_cents),
            )
            .where(
                InvestmentSaleRecord.workspace_id == workspace_id,
                InvestmentSaleRecord.status == EvidenceStatus.VERIFIED,
            )
            .group_by(InvestmentSaleRecord.term)
        )
        rows = self.session.exec(stmt).all()
        summary = {
            "short_term_proceeds_cents": 0,
            "short_term_basis_cents": 0,
            "short_term_gain_loss_cents": 0,
            "short_term_wash_sale_disallowed_cents": 0,
            "long_term_proceeds_cents": 0,
            "long_term_basis_cents": 0,
            "long_term_gain_loss_cents": 0,
            "long_term_wash_sale_disallowed_cents": 0,
        }
        for term, proceeds, basis, gain, wash in rows:
            prefix = "short_term" if term == "SHORT_TERM" else "long_term"
            summary[f"{prefix}_proceeds_cents"] = int(proceeds or 0)
            summary[f"{prefix}_basis_cents"] = int(basis or 0)
            summary[f"{prefix}_gain_loss_cents"] = int(gain or 0)
            summary[f"{prefix}_wash_sale_disallowed_cents"] = int(wash or 0)
        return summary

    # -------------------------------------------------------------------------
    # Tax Questions & Reconciliations
    # -------------------------------------------------------------------------

    def create_tax_question(
        self,
        workspace_id: str,
        question_text: str,
        context: str | None = None,
    ) -> TaxQuestion:
        """Create an open tax question blocking final approval."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        q = TaxQuestion(
            workspace_id=workspace_id,
            question_text=question_text,
            context=context,
            status="OPEN",
        )
        self.session.add(q)
        self.session.commit()
        self.session.refresh(q)
        return q

    def resolve_tax_question(
        self,
        question_id: str,
        resolution: str,
        authority_citation: str | None = None,
    ) -> TaxQuestion:
        """Resolve a tax question with explanation and statutory authority."""
        q = self.session.get(TaxQuestion, question_id)
        if q is None:
            raise EntityNotFoundError(f"TaxQuestion {question_id} not found")

        workspace = self._require_workspace(q.workspace_id)
        self._check_not_locked(workspace)

        q.status = "ANSWERED"
        q.resolution = resolution
        q.authority_citation = authority_citation
        q.resolved_at = _utcnow()
        self.session.add(q)
        self.session.commit()
        self.session.refresh(q)
        return q

    def record_reconciliation_item(
        self,
        workspace_id: str,
        tax_section: str,
        field_key: str,
        source_working_paper_cents: int,
        taxact_entered_cents: int,
        delta_cents: int,
        status: str = ReconciliationStatus.UNRESOLVED,
        explanation: str = "",
        authority_citation: str = "",
        consumed_transaction_ids: Sequence[int] | None = None,
    ) -> ReconciliationItem:
        """Record an audited comparison between working paper and TaxAct entry."""
        workspace = self._require_workspace(workspace_id)
        self._check_not_locked(workspace)

        tx_json = json.dumps(list(consumed_transaction_ids)) if consumed_transaction_ids else None
        item = ReconciliationItem(
            workspace_id=workspace_id,
            tax_section=tax_section,
            field_key=field_key,
            source_working_paper_cents=source_working_paper_cents,
            taxact_entered_cents=taxact_entered_cents,
            delta_cents=delta_cents,
            status=status,
            explanation=explanation,
            authority_citation=authority_citation,
            consumed_transaction_ids_json=tx_json,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    # -------------------------------------------------------------------------
    # Invalidation Cascades for Ledger Edits (INV-10)
    # -------------------------------------------------------------------------

    def invalidate_for_transaction_edit(self, transaction_id: int) -> int:
        """Invalidate reconciliation items that consumed `transaction_id`.

        Marks affected ReconciliationItems STALE and affected EvidencePackets stale.
        Returns the count of invalidated reconciliation items.
        """
        # Find all reconciliation items with consumed_transaction_ids_json
        stmt = select(ReconciliationItem).where(
            ReconciliationItem.consumed_transaction_ids_json.is_not(None),
            ReconciliationItem.status != ReconciliationStatus.STALE,
        )
        items = self.session.exec(stmt).all()
        invalidated_count = 0
        affected_workspaces: set[str] = set()

        for item in items:
            if not item.consumed_transaction_ids_json:
                continue
            try:
                tx_ids = json.loads(item.consumed_transaction_ids_json)
                if transaction_id in tx_ids:
                    item.status = ReconciliationStatus.STALE
                    self.session.add(item)
                    invalidated_count += 1
                    affected_workspaces.add(item.workspace_id)
            except Exception:
                continue

        for ws_id in affected_workspaces:
            packets_stmt = select(EvidencePacket).where(
                EvidencePacket.workspace_id == ws_id,
                EvidencePacket.is_stale == False,  # noqa: E712
            )
            for pkt in self.session.exec(packets_stmt).all():
                pkt.is_stale = True
                self.session.add(pkt)

        if invalidated_count:
            self.session.commit()

        return invalidated_count

    # -------------------------------------------------------------------------
    # Evidence Packet Snapshot Creation
    # -------------------------------------------------------------------------

    def create_evidence_packet(
        self,
        workspace_id: str,
        is_draft: bool = True,
    ) -> EvidencePacket:
        """Create an immutable evidence packet snapshot with content hash."""
        workspace = self._require_workspace(workspace_id)

        # Gather state snapshot
        verified_fields_stmt = select(ExtractedField).where(
            ExtractedField.workspace_id == workspace_id,
            ExtractedField.status == EvidenceStatus.VERIFIED,
        )
        fields = self.session.exec(verified_fields_stmt).all()

        docs_stmt = select(TaxDocument).where(TaxDocument.workspace_id == workspace_id)
        docs = self.session.exec(docs_stmt).all()

        questions_stmt = select(TaxQuestion).where(TaxQuestion.workspace_id == workspace_id)
        questions = self.session.exec(questions_stmt).all()

        snapshot_data = {
            "workspace_id": workspace_id,
            "tax_year": workspace.tax_year,
            "jurisdiction": workspace.jurisdiction,
            "snapshot_at": _utcnow().isoformat(),
            "documents": [
                {
                    "id": d.id,
                    "file_name": d.file_name,
                    "sha256": d.sha256_hash,
                    "doc_type": d.doc_type,
                    "is_corrected": d.is_corrected,
                }
                for d in docs
            ],
            "verified_fields": [
                {
                    "id": f.id,
                    "field_name": f.field_name,
                    "amount_cents": f.amount_cents,
                    "raw_text": f.raw_text,
                    "document_id": f.document_id,
                    "box_or_line": f.box_or_line,
                    "page_number": f.page_number,
                    "verified_by": f.verified_by,
                }
                for f in fields
            ],
            "questions": [
                {
                    "id": q.id,
                    "status": q.status,
                    "question_text": q.question_text,
                    "resolution": q.resolution,
                }
                for q in questions
            ],
        }

        canonical_json = json.dumps(snapshot_data, sort_keys=True)
        content_hash = compute_sha256(canonical_json.encode("utf-8"))

        packet = EvidencePacket(
            workspace_id=workspace_id,
            generated_at=_utcnow(),
            content_hash=content_hash,
            is_draft=is_draft,
            is_stale=False,
            snapshot_json=canonical_json,
        )
        self.session.add(packet)
        self.session.commit()
        self.session.refresh(packet)
        return packet
