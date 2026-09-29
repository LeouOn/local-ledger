"""Tax Document Intake and Manual Verification Service (TAX-05).

Provides:
1. Expected vs. missing document inventory tracking for annual tax audits.
2. Safe local file intake: strict size and format validation, immutable private storage.
3. Content-addressable hash deduplication: re-uploading identical bytes returns the
   existing record without duplicating approved amounts.
4. Text PDF page-by-page extraction and scanned document detection (`scanned_no_text`).
5. Manual field entry and verification lifecycle review interface.
6. Zero half-registered approved records on parsing or validation failures.
"""

from __future__ import annotations

import contextlib
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from local_ledger.tax.models import (
    DocumentType,
    EvidenceStatus,
    ExtractedField,
    ScanState,
    TaxDocument,
)
from local_ledger.tax.service import (
    TaxWorkspaceService,
)

MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB limit
SUPPORTED_EXTENSIONS = {".pdf", ".json", ".csv", ".txt", ".png", ".jpg", ".jpeg"}


class IntakeError(Exception):
    """Base exception for document intake errors."""


class FileOversizedError(IntakeError):
    """Raised when an ingested file exceeds the size limit."""


class UnsupportedFileTypeError(IntakeError):
    """Raised when an unsupported file format is provided."""


class EmptyFileError(IntakeError):
    """Raised when an uploaded file is empty (0 bytes)."""


class DocumentParsingError(IntakeError):
    """Raised when a document cannot be parsed or read."""


@dataclass(frozen=True)
class ExpectedDocCategory:
    doc_type: str
    title: str
    description: str
    is_required: bool


EXPECTED_2025_CATEGORIES = [
    ExpectedDocCategory(
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
        title="Consolidated Form 1099",
        description="Brokerage 1099 (INT, DIV, and B security dispositions)",
        is_required=True,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.RENTAL_ANNUAL_STATEMENT,
        title="Rental Annual Operating Statement",
        description="Property manager or owner annual Schedule E income & expense statement",
        is_required=True,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.FORM_1098_MORTGAGE,
        title="Form 1098 Mortgage Interest",
        description="Mortgage interest, points, and property tax escrow payments",
        is_required=True,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.PROPERTY_TAX_BILL,
        title="Property Tax Assessment/Bill",
        description="County property tax bills showing assessed land vs. improvement value",
        is_required=True,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.PRIOR_YEAR_RETURN_FED,
        title="2024 Federal Return (Form 1040)",
        description="Prior-year return for capital loss and passive loss carryover verification",
        is_required=False,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.PRIOR_YEAR_RETURN_CA,
        title="2024 California Return (Form 540)",
        description="Prior-year California return for Schedule CA state carryovers",
        is_required=False,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.CLOSING_STATEMENT_HUD1,
        title="Settlement / Closing Statement",
        description="HUD-1 or Alta settlement statement for property basis & acquisition costs",
        is_required=False,
    ),
    ExpectedDocCategory(
        doc_type=DocumentType.OTHER_EVIDENCE,
        title="Other Supporting Evidence",
        description="Capital improvement receipts, utility invoices, or repair documentation",
        is_required=False,
    ),
]


@dataclass
class InventoryStatus:
    doc_type: str
    title: str
    description: str
    is_required: bool
    status: str  # "PRESENT", "MISSING", "SUPERSEDED"
    active_document_count: int
    documents: list[TaxDocument] = field(default_factory=list)


@dataclass
class IntakeUploadResult:
    success: bool
    document: TaxDocument | None
    is_duplicate: bool = False
    scan_state: str = ScanState.TEXT_EXTRACTABLE
    page_count: int = 1
    page_texts: dict[int, str] = field(default_factory=dict)
    error_message: str | None = None


def get_inventory_status(session: Session, workspace_id: str) -> list[InventoryStatus]:
    """Compile expected vs. missing document inventory for a tax workspace."""
    docs_stmt = (
        select(TaxDocument)
        .where(TaxDocument.workspace_id == workspace_id)
        .order_by(TaxDocument.ingested_at.desc())  # type: ignore[attr-defined]
    )
    all_docs = list(session.exec(docs_stmt).all())

    # Map documents by doc_type
    docs_by_type: dict[str, list[TaxDocument]] = {}
    for d in all_docs:
        docs_by_type.setdefault(d.doc_type, []).append(d)

    # Find IDs of superseded documents
    superseded_ids = {d.supersedes_doc_id for d in all_docs if d.supersedes_doc_id}

    inventory: list[InventoryStatus] = []
    for cat in EXPECTED_2025_CATEGORIES:
        matched_docs = docs_by_type.get(cat.doc_type, [])
        active_docs = [d for d in matched_docs if d.id not in superseded_ids]

        if active_docs:
            status = "PRESENT"
        elif matched_docs and all(d.id in superseded_ids for d in matched_docs):
            status = "SUPERSEDED"
        else:
            status = "MISSING"

        inventory.append(
            InventoryStatus(
                doc_type=cat.doc_type,
                title=cat.title,
                description=cat.description,
                is_required=cat.is_required,
                status=status,
                active_document_count=len(active_docs),
                documents=matched_docs,
            )
        )

    # Add any uploaded documents whose type is not in standard categories
    known_types = {c.doc_type for c in EXPECTED_2025_CATEGORIES}
    for doc_type, docs in docs_by_type.items():
        if doc_type not in known_types:
            active_docs = [d for d in docs if d.id not in superseded_ids]
            inventory.append(
                InventoryStatus(
                    doc_type=doc_type,
                    title=f"Custom: {doc_type}",
                    description="Additional user-uploaded tax evidence",
                    is_required=False,
                    status="PRESENT" if active_docs else "SUPERSEDED",
                    active_document_count=len(active_docs),
                    documents=docs,
                )
            )

    return inventory


def extract_pdf_page_texts(pdf_bytes: bytes) -> tuple[str, int, dict[int, str]]:
    """Extract page count and text layer per page from a PDF.

    Returns:
        (scan_state, page_count, {page_num: text})
    """
    if not pdf_bytes.startswith(b"%PDF"):
        # Not a PDF
        return ScanState.UNSUPPORTED_TYPE, 1, {1: ""}

    # 1. Page count estimation via /Type /Page
    page_matches = re.findall(rb"/Type\s*/Page\b", pdf_bytes)
    page_count = max(len(page_matches), 1)

    # 2. Extract streams
    stream_matches = re.findall(rb"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL)
    decompressed_streams: list[bytes] = []

    for s in stream_matches:
        try:
            data = zlib.decompress(s)
            decompressed_streams.append(data)
        except Exception:
            # If not zlib-compressed, check if it's raw text
            if b"BT" in s and b"ET" in s:
                decompressed_streams.append(s)

    # 3. Extract text from text operators: (string) Tj or [(string)...] TJ
    extracted_tokens: list[str] = []
    for stream in decompressed_streams:
        # Match (text) Tj
        tj_matches = re.findall(rb"\((.*?)\)\s*(?:Tj|')", stream)
        for m in tj_matches:
            with contextlib.suppress(Exception):
                extracted_tokens.append(m.decode("latin-1"))

        # Match [(str1) (str2)] TJ
        tj_array_matches = re.findall(rb"\[(.*?)\]\s*TJ", stream)
        for arr in tj_array_matches:
            items = re.findall(rb"\((.*?)\)", arr)
            for it in items:
                with contextlib.suppress(Exception):
                    extracted_tokens.append(it.decode("latin-1"))

    total_text = " ".join(extracted_tokens).strip()

    if not total_text or len(total_text) < 5:
        # Document is a scanned PDF without text layer
        pages_dict = {
            p: f"(Scanned page {p} - no embedded text layer detected. Please use Manual Field Entry below.)"
            for p in range(1, page_count + 1)
        }
        return ScanState.SCANNED_NO_TEXT, page_count, pages_dict

    # Text is extractable. If single stream, assign to page 1; otherwise distribute
    pages_dict = {}
    if len(decompressed_streams) >= page_count:
        for idx in range(page_count):
            stream = decompressed_streams[idx]
            page_tokens = []
            for m in re.findall(rb"\((.*?)\)\s*(?:Tj|')", stream):
                with contextlib.suppress(Exception):
                    page_tokens.append(m.decode("latin-1"))
            for arr in re.findall(rb"\[(.*?)\]\s*TJ", stream):
                for it in re.findall(rb"\((.*?)\)", arr):
                    with contextlib.suppress(Exception):
                        page_tokens.append(it.decode("latin-1"))
            page_text = " ".join(page_tokens).strip() or total_text
            pages_dict[idx + 1] = page_text
    else:
        pages_dict = {1: total_text}
        for p in range(2, page_count + 1):
            pages_dict[p] = f"(Page {p} text)"

    return ScanState.TEXT_EXTRACTABLE, page_count, pages_dict


def inspect_file_content(file_bytes: bytes, file_name: str) -> tuple[str, int, dict[int, str]]:
    """Determine scan state, page count, and text preview for uploaded file bytes."""
    ext = Path(file_name).suffix.lower()

    if ext == ".pdf":
        return extract_pdf_page_texts(file_bytes)

    if ext in {".json", ".csv", ".txt"}:
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")
        return ScanState.TEXT_EXTRACTABLE, 1, {1: text}

    if ext in {".png", ".jpg", ".jpeg"}:
        return (
            ScanState.SCANNED_NO_TEXT,
            1,
            {1: "(Image document — text extraction not available without OCR. Use manual field entry.)"},
        )

    return ScanState.UNSUPPORTED_TYPE, 1, {1: "(Unsupported format)"}


def validate_file_upload(file_name: str, file_bytes: bytes) -> None:
    """Validate file size, extension, and content non-emptiness.

    Raises:
        EmptyFileError, FileOversizedError, UnsupportedFileTypeError.
    """
    if not file_bytes:
        raise EmptyFileError("Uploaded file is empty (0 bytes).")

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        mb = len(file_bytes) / (1024 * 1024)
        raise FileOversizedError(f"File size {mb:.1f} MB exceeds 25 MB limit.")

    ext = Path(file_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"File format '{ext}' is unsupported. Allowed: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def ingest_document(
    session: Session,
    workspace_id: str,
    file_name: str,
    file_bytes: bytes,
    doc_type: str = DocumentType.OTHER_EVIDENCE,
    is_corrected: bool = False,
    supersedes_doc_id: str | None = None,
) -> IntakeUploadResult:
    """Safely ingest an uploaded document into the tax workspace.

    Guarantees:
    - Atomicity: validation failures leave zero records behind.
    - Immutability: stored into private external tax storage with SHA-256 integrity.
    - Deduplication: re-uploading identical bytes returns existing document with `is_duplicate=True`.
    - Supersession: corrected uploads invalidate downstream items atomically.
    """
    try:
        validate_file_upload(file_name, file_bytes)
    except IntakeError as err:
        return IntakeUploadResult(success=False, document=None, error_message=str(err))

    service = TaxWorkspaceService(session)

    # Inspect text and pages before registration
    scan_state, page_count, pages_dict = inspect_file_content(file_bytes, file_name)

    try:
        # Check if identical hash already exists in this workspace
        from local_ledger.tax.storage import compute_sha256

        incoming_hash = compute_sha256(file_bytes)
        existing_doc = session.exec(
            select(TaxDocument).where(
                TaxDocument.workspace_id == workspace_id,
                TaxDocument.sha256_hash == incoming_hash,
            )
        ).first()

        if existing_doc is not None:
            # Duplicate upload: per contract §11, return existing without duplicating approved amounts
            return IntakeUploadResult(
                success=True,
                document=existing_doc,
                is_duplicate=True,
                scan_state=existing_doc.scan_state,
                page_count=existing_doc.page_count,
                page_texts=pages_dict,
                error_message=f"Duplicate file: identical content already registered as {existing_doc.file_name}.",
            )

        # Register document via TaxWorkspaceService
        doc = service.register_document(
            workspace_id=workspace_id,
            file_name=file_name,
            source=file_bytes,
            doc_type=doc_type,
            is_corrected=is_corrected,
            supersedes_doc_id=supersedes_doc_id,
        )

        # Update detected scan state
        doc.scan_state = scan_state
        doc.page_count = page_count
        session.add(doc)
        session.commit()
        session.refresh(doc)

        return IntakeUploadResult(
            success=True,
            document=doc,
            is_duplicate=False,
            scan_state=scan_state,
            page_count=page_count,
            page_texts=pages_dict,
        )

    except Exception as exc:
        return IntakeUploadResult(success=False, document=None, error_message=str(exc))


def add_manual_field(
    session: Session,
    workspace_id: str,
    document_id: str,
    field_name: str,
    raw_text: str,
    amount_cents: int,
    page_number: int = 1,
    box_or_line: str = "",
    snippet_text: str = "",
    notes: str | None = None,
) -> ExtractedField:
    """Record a manually entered field for a document."""
    service = TaxWorkspaceService(session)
    field_record = service.propose_field(
        workspace_id=workspace_id,
        document_id=document_id,
        field_name=field_name,
        raw_text=raw_text,
        amount_cents=amount_cents,
        page_number=page_number,
        box_or_line=box_or_line,
        snippet_text=snippet_text,
    )
    if notes:
        field_record.notes = notes
        session.add(field_record)
        session.commit()
        session.refresh(field_record)
    return field_record


def review_field(
    session: Session,
    field_id: str,
    action: str,  # "VERIFY" | "REJECT"
    reviewer: str,
    notes: str | None = None,
) -> ExtractedField:
    """Apply a reviewer verification or rejection action to an extracted field."""
    service = TaxWorkspaceService(session)
    action_upper = action.upper()
    if action_upper == "VERIFY":
        return service.verify_field(field_id=field_id, verified_by=reviewer, notes=notes)
    if action_upper == "REJECT":
        return service.reject_field(
            field_id=field_id,
            rejected_by=reviewer,
            notes=notes or "Rejected during manual review",
        )
    raise ValueError(f"Unknown review action '{action}'. Must be 'VERIFY' or 'REJECT'.")


def get_workspace_fields_summary(session: Session, workspace_id: str) -> dict[str, Any]:
    """Summary of all extracted fields and verified vs unverified totals."""
    stmt = (
        select(ExtractedField)
        .where(ExtractedField.workspace_id == workspace_id)
        .order_by(ExtractedField.field_name)  # type: ignore[attr-defined]
    )
    fields = list(session.exec(stmt).all())

    verified_cents = sum(f.amount_cents for f in fields if f.status == EvidenceStatus.VERIFIED)
    proposed_cents = sum(f.amount_cents for f in fields if f.status == EvidenceStatus.PROPOSED)
    rejected_cents = sum(f.amount_cents for f in fields if f.status == EvidenceStatus.REJECTED)
    superseded_cents = sum(f.amount_cents for f in fields if f.status == EvidenceStatus.SUPERSEDED)

    return {
        "fields": fields,
        "total_fields": len(fields),
        "verified_cents": verified_cents,
        "proposed_cents": proposed_cents,
        "rejected_cents": rejected_cents,
        "superseded_cents": superseded_cents,
        "verified_count": sum(1 for f in fields if f.status == EvidenceStatus.VERIFIED),
        "proposed_count": sum(1 for f in fields if f.status == EvidenceStatus.PROPOSED),
        "rejected_count": sum(1 for f in fields if f.status == EvidenceStatus.REJECTED),
        "superseded_count": sum(1 for f in fields if f.status == EvidenceStatus.SUPERSEDED),
    }
