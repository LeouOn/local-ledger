"""SQLModel tables for Tax Workspace and Evidence Lifecycle."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import ConfigDict, field_validator
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _validate_iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError("date must be a YYYY-MM-DD string")
    date.fromisoformat(value)
    return value


def _validate_int_cents(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("amount_cents must be an int, not bool")
    if isinstance(value, float):
        raise ValueError("amount_cents must be an int (cents); floats are forbidden")
    if not isinstance(value, int):
        raise ValueError("amount_cents must be an int")
    return value


def _validate_optional_int_cents(value: object) -> int | None:
    if value is None:
        return None
    return _validate_int_cents(value)


class DocumentType(StrEnum):
    FORM_1099_CONSOLIDATED = "1099_CONSOLIDATED"
    FORM_1099_B = "1099_B"
    FORM_1099_DIV = "1099_DIV"
    FORM_1099_INT = "1099_INT"
    FORM_1098_MORTGAGE = "1098_MORTGAGE"
    RENTAL_ANNUAL_STATEMENT = "RENTAL_ANNUAL_STATEMENT"
    PROPERTY_TAX_BILL = "PROPERTY_TAX_BILL"
    PRIOR_YEAR_RETURN_FED = "PRIOR_YEAR_RETURN_FED"
    PRIOR_YEAR_RETURN_CA = "PRIOR_YEAR_RETURN_CA"
    CLOSING_STATEMENT_HUD1 = "CLOSING_STATEMENT_HUD1"
    OTHER_EVIDENCE = "OTHER_EVIDENCE"


class EvidenceStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    DISCREPANCY = "DISCREPANCY"
    UNRESOLVED = "UNRESOLVED"
    STALE = "STALE"


class HoldingTerm(StrEnum):
    SHORT_TERM = "SHORT_TERM"
    LONG_TERM = "LONG_TERM"


class Jurisdiction(StrEnum):
    FEDERAL = "US-FED"
    CALIFORNIA = "US-CA"


class ResidencyStatus(StrEnum):
    """Residency is a fact the user must confirm; UNKNOWN is the honest default."""

    UNKNOWN = "UNKNOWN"
    FULL_YEAR = "FULL_YEAR"
    PART_YEAR = "PART_YEAR"
    NON_RESIDENT = "NON_RESIDENT"


class FilingStatus(StrEnum):
    """Filing status is a fact the user must confirm; UNKNOWN is the honest default."""

    UNKNOWN = "UNKNOWN"
    SINGLE = "SINGLE"
    MARRIED_FILING_JOINTLY = "MARRIED_FILING_JOINTLY"
    MARRIED_FILING_SEPARATELY = "MARRIED_FILING_SEPARATELY"
    HEAD_OF_HOUSEHOLD = "HEAD_OF_HOUSEHOLD"
    QUALIFYING_SURVIVING_SPOUSE = "QUALIFYING_SURVIVING_SPOUSE"


class WorkspaceStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    LOCKED = "LOCKED"


class ScanState(StrEnum):
    TEXT_EXTRACTABLE = "text_extractable"
    SCANNED_NO_TEXT = "scanned_no_text"
    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    HASH_DUPLICATE = "hash_duplicate"
    WORKSPACE_MISMATCH = "workspace_mismatch"


class TaxWorkspace(SQLModel, table=True):
    """Annual tax preparation review workspace.

    ``residency_status`` and ``filing_status`` default to UNKNOWN, not to a
    specific answer: neither fact has been confirmed, and a default that reads
    as a fact is how a wrong assumption reaches a return.
    """

    model_config = ConfigDict(validate_assignment=True)
    __table_args__ = (
        UniqueConstraint("tax_year", "jurisdiction", name="uq_tax_workspace_year_jurisdiction"),
    )

    id: str = Field(default_factory=lambda: f"ws-{uuid.uuid4().hex[:12]}", primary_key=True)
    tax_year: int = Field(index=True)
    jurisdiction: str = Field(default=Jurisdiction.FEDERAL, index=True)
    residency_status: str = Field(default=ResidencyStatus.UNKNOWN)
    filing_status: str = Field(default=FilingStatus.UNKNOWN)
    status: str = Field(default=WorkspaceStatus.DRAFT, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class TaxDocument(SQLModel, table=True):
    """Immutable metadata for ingested tax source documents."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"doc-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    file_name: str
    sha256_hash: str = Field(index=True)
    doc_type: str = Field(default=DocumentType.OTHER_EVIDENCE, index=True)
    page_count: int = Field(default=1)
    scan_state: str = Field(default=ScanState.TEXT_EXTRACTABLE, index=True)
    is_corrected: bool = Field(default=False)
    supersedes_doc_id: str | None = Field(default=None, foreign_key="taxdocument.id")
    storage_path: str = Field(default="")
    ingested_at: datetime = Field(default_factory=_utcnow)


class ExtractedField(SQLModel, table=True):
    """Extracted amount or field proposed from a tax document."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"fld-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    document_id: str = Field(foreign_key="taxdocument.id", index=True)
    page_number: int = Field(default=1)
    box_or_line: str = Field(default="")
    snippet_text: str = Field(default="")
    bounding_box_json: str | None = Field(default=None)
    field_name: str = Field(index=True)
    raw_text: str = Field(default="")
    amount_cents: int = Field(default=0)
    status: str = Field(default=EvidenceStatus.PROPOSED, index=True)
    model_provenance_json: str | None = Field(default=None)
    verified_by: str | None = Field(default=None)
    verified_at: datetime | None = Field(default=None)
    notes: str | None = Field(default=None)

    @field_validator("amount_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)


class TaxProperty(SQLModel, table=True):
    """Rental real estate property record."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"prop-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    street_address: str
    city: str
    state: str
    zip_code: str
    property_type: str = Field(default="Single Family")
    ownership_basis_points: int = Field(default=10000)
    days_rented: int = Field(default=365)
    days_personal_use: int = Field(default=0)
    date_placed_in_service: str = Field(default="")

    @field_validator("date_placed_in_service")
    @classmethod
    def _check_service_date(cls, v: str) -> str:
        if v:
            _validate_iso_date(v)
        return v


class RentalExpenseRecord(SQLModel, table=True):
    """Operating expense item linked to rental property and source evidence."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"exp-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    property_id: str = Field(foreign_key="taxproperty.id", index=True)
    category: str = Field(index=True)
    amount_cents: int = Field(default=0)
    raw_text: str = Field(default="")
    source_document_id: str | None = Field(default=None, foreign_key="taxdocument.id")
    source_transaction_id: int | None = Field(default=None, foreign_key="transaction.id")
    page_number: int | None = Field(default=None)
    box_or_line: str | None = Field(default=None)
    treatment_authority: str = Field(default="IRC § 162 / IRS Pub 527")
    is_capital_improvement_suspect: bool = Field(default=False)
    status: str = Field(default=EvidenceStatus.PROPOSED, index=True)

    @field_validator("amount_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)


class AssetDepreciation(SQLModel, table=True):
    """Asset depreciation schedules under federal MACRS and California rules."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"ast-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    property_id: str = Field(foreign_key="taxproperty.id", index=True)
    description: str
    date_placed_in_service: str
    cost_basis_cents: int
    land_value_cents: int = Field(default=0)
    depreciable_basis_cents: int
    recovery_period_months: int = Field(default=330)  # 330 months for 27.5-year residential rental
    prior_depreciation_fed_cents: int = Field(default=0)
    prior_depreciation_ca_cents: int = Field(default=0)
    current_depreciation_fed_cents: int = Field(default=0)
    current_depreciation_ca_cents: int = Field(default=0)
    state_difference_reason: str = Field(default="MACRS vs CA Modified S/L")

    @property
    def recovery_years(self) -> float:
        """Helper returning recovery period in years (e.g. 27.5)."""
        return self.recovery_period_months / 12.0

    @field_validator("cost_basis_cents", "land_value_cents", "depreciable_basis_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)

    @field_validator("date_placed_in_service")
    @classmethod
    def _check_date(cls, v: str) -> str:
        res = _validate_iso_date(v)
        assert res is not None
        return res


class InvestmentSaleRecord(SQLModel, table=True):
    """Security/stock disposition record for Form 8949 / Schedule D audit."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"sec-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    source_document_id: str = Field(foreign_key="taxdocument.id", index=True)
    security_description: str
    cusip_or_ticker: str | None = Field(default=None)
    date_acquired: str | None = Field(default=None)
    date_sold: str
    proceeds_cents: int
    cost_basis_cents: int | None = Field(default=None)  # None indicates missing/noncovered basis
    gain_loss_cents: int | None = Field(default=None)    # None indicates undetermined gain/loss
    term: str = Field(default=HoldingTerm.SHORT_TERM)
    basis_reported_to_irs: bool = Field(default=True)
    wash_sale_disallowed_cents: int = Field(default=0)
    fed_ca_difference_cents: int = Field(default=0)
    status: str = Field(default=EvidenceStatus.PROPOSED, index=True)
    raw_text: str = Field(default="")

    @field_validator("proceeds_cents", "wash_sale_disallowed_cents", "fed_ca_difference_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)

    @field_validator("cost_basis_cents", "gain_loss_cents", mode="before")
    @classmethod
    def _check_optional_cents(cls, v: object) -> int | None:
        return _validate_optional_int_cents(v)

    @field_validator("date_sold")
    @classmethod
    def _check_sold_date(cls, v: str) -> str:
        res = _validate_iso_date(v)
        assert res is not None
        return res

    @field_validator("date_acquired")
    @classmethod
    def _check_acquired_date(cls, v: str | None) -> str | None:
        return _validate_iso_date(v)


class JurisdictionCarryover(SQLModel, table=True):
    """Federal vs. California prior-year carryovers."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"cry-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    jurisdiction: str = Field(default=Jurisdiction.FEDERAL, index=True)
    category: str = Field(default="CAPITAL_LOSS", index=True)
    property_id: str | None = Field(default=None)
    prior_year_cents: int | None = Field(default=None)  # None indicates missing prior return
    utilized_current_year_cents: int = Field(default=0)
    remaining_carryover_cents: int | None = Field(default=None)
    prior_return_reference: str = Field(default="")

    @field_validator("utilized_current_year_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)

    @field_validator("prior_year_cents", "remaining_carryover_cents", mode="before")
    @classmethod
    def _check_optional_cents(cls, v: object) -> int | None:
        return _validate_optional_int_cents(v)


class TaxQuestion(SQLModel, table=True):
    """Unresolved tax treatment or verification question."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"qst-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    question_text: str
    context: str | None = Field(default=None)
    status: str = Field(default="OPEN", index=True)
    resolution: str | None = Field(default=None)
    resolved_at: datetime | None = Field(default=None)
    authority_citation: str | None = Field(default=None)


class ReconciliationItem(SQLModel, table=True):
    """Audited comparison item between source working papers and TaxAct entries."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"rec-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    tax_section: str
    field_key: str = Field(index=True)
    source_working_paper_cents: int = Field(default=0)
    taxact_entered_cents: int = Field(default=0)
    delta_cents: int = Field(default=0)
    status: str = Field(default=ReconciliationStatus.UNRESOLVED, index=True)
    explanation: str = Field(default="")
    authority_citation: str = Field(default="")
    consumed_transaction_ids_json: str | None = Field(default=None)

    @field_validator("source_working_paper_cents", "taxact_entered_cents", "delta_cents", mode="before")
    @classmethod
    def _check_cents(cls, v: object) -> int:
        return _validate_int_cents(v)


class EvidencePacket(SQLModel, table=True):
    """Immutable audit evidence packet snapshot."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"pkt-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(foreign_key="taxworkspace.id", index=True)
    generated_at: datetime = Field(default_factory=_utcnow)
    content_hash: str = Field(default="", index=True)
    is_draft: bool = Field(default=True)
    is_stale: bool = Field(default=False)
    snapshot_json: str = Field(default="{}")
