"""SQLModel domain tables for Local Ledger.

Money is stored as integer cents. Dates are naive ``YYYY-MM-DD`` strings. There
is no mutable carryover column - carryover is computed on read (todo 6). The
``actor`` columns (Transaction.created_by, AuditLog.actor) record provenance as
``user`` | ``llm:<provider>:<model>`` | ``import:<source>``.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import ConfigDict, field_validator
from sqlalchemy import CheckConstraint, Index, text
from sqlmodel import Field, SQLModel

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AccountType(StrEnum):
    checking = "checking"
    savings = "savings"
    credit = "credit"
    cash = "cash"


class DedupStatus(StrEnum):
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    NEW = "NEW"


class ReviewState(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


def _validate_iso_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError("date must be a YYYY-MM-DD string")
    # Validate it is a real calendar date.
    date.fromisoformat(value)
    return value


def _validate_month(value: str) -> str:
    if not isinstance(value, str) or not _MONTH_RE.match(value):
        raise ValueError("month must be a YYYY-MM string")
    _year, month = value.split("-")
    if not (1 <= int(month) <= 12):
        raise ValueError("month must be a YYYY-MM string")
    return value


def _validate_int_cents(value: object) -> int:
    # Reject any float, including whole-number floats, to keep money strict.
    if isinstance(value, bool):  # bool is a subclass of int - disallow.
        raise ValueError("amount_cents must be an int, not bool")
    if isinstance(value, float):
        raise ValueError("amount_cents must be an int (cents); floats are forbidden")
    if not isinstance(value, int):
        raise ValueError("amount_cents must be an int")
    return value


class Account(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    type: AccountType = Field(default=AccountType.checking)
    currency: str = Field(default="USD", description="App-wide constant in v1")
    created_at: datetime = Field(default_factory=_utcnow)


class Payee(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    normalized_name: str


class CategoryGroup(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Category(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    group_id: int | None = Field(default=None, foreign_key="categorygroup.id")
    name: str
    is_income: bool = False


class Transaction(SQLModel, table=True):
    # SQLModel table models skip validation on construction by default; enabling
    # validate_assignment also makes __init__ run the field validators, which is
    # what enforces strict integer-cents and ISO-date invariants.
    model_config = ConfigDict(validate_assignment=True)
    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="ck_transaction_amount_nonzero"),
        Index(
            "uq_transaction_account_imported_id",
            "account_id",
            "imported_id",
            unique=True,
            sqlite_where=text("imported_id IS NOT NULL"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    date: str
    amount_cents: int
    payee_id: int | None = Field(default=None, foreign_key="payee.id")
    category_id: int | None = Field(default=None, foreign_key="category.id")
    notes: str | None = None
    imported_id: str | None = Field(default=None, index=True)
    transferred_id: int | None = Field(
        default=None, foreign_key="transaction.id", description="Mirror row for transfers"
    )
    is_reconciled: bool = False
    created_by: str = Field(default="user", description="user | llm:... | import:...")
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("amount_cents", mode="before")
    @classmethod
    def _check_int_cents(cls, v: object) -> int:
        return _validate_int_cents(v)

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_iso_date(v)


class CategoryBudget(SQLModel, table=True):
    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="category.id")
    month: str  # YYYY-MM
    allocated_cents: int
    carryover_enabled: bool = False

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: str) -> str:
        return _validate_month(v)

    @field_validator("allocated_cents", mode="before")
    @classmethod
    def _check_int(cls, v: object) -> int:
        return _validate_int_cents(v)


class Recommendation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow)
    provider: str
    model: str
    prompt_hash: str
    month: str
    body: str  # append-only; never updated via service (todo 18)
    followed: bool | None = Field(default=None, description="User-set only")

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: str) -> str:
        return _validate_month(v)


class AuditLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    entity_type: str
    entity_id: int | None = None
    action: str  # create | update | delete | egress
    actor: str  # user | llm:<provider>:<model> | import:<source>
    before_json: str | None = None
    after_json: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class ProviderSettings(SQLModel, table=True):
    """Singleton row (id=1) holding the app-wide provider configuration."""

    id: int = Field(default=1, primary_key=True)
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = "llama3.3:8b"
    local_profile: str = Field(
        default="ollama", description="Selected local server profile: ollama | ds4 | glm | glmo"
    )
    local_autostart: bool = Field(
        default=False, description="Start the selected local server when none is healthy"
    )
    cloud_base_url: str | None = None
    cloud_model: str | None = None
    cloud_api_key_env: str | None = Field(
        default=None, description="Name of env var holding the cloud key (never the key)"
    )
    evaluation_default_provider: str = Field(default="local")  # local | cloud-consent
    theme_mode: str = Field(default="light", description="light | dark")


class ImportPrefs(SQLModel, table=True):
    account_id: int = Field(primary_key=True, foreign_key="account.id")
    mapping_json: str


class StagedTransaction(SQLModel, table=True):
    """Rows produced by ingest, awaiting dedup classification and user confirm.

    Provenance columns: ``import_batch`` + ``source_row`` locate the row in the
    originating file, and ``committed_transaction_id`` records the transaction
    this row produced so a repeated commit is a no-op instead of a duplicate.
    """

    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    import_batch: str
    account_id: int = Field(foreign_key="account.id")
    date: str
    amount_cents: int
    payee_raw: str | None = None
    notes: str | None = None
    source_row: int | None = Field(default=None, description="1-based line in the source file")
    suggested_category_id: int | None = Field(default=None, foreign_key="category.id")
    confidence: float | None = None
    dedup_status: DedupStatus = Field(default=DedupStatus.NEW)
    review_state: ReviewState = Field(default=ReviewState.PENDING)
    imported_id: str | None = None
    committed_transaction_id: int | None = Field(
        default=None, foreign_key="transaction.id", description="Set once this row is committed"
    )
    committed_at: datetime | None = Field(default=None)
    committed_by: str | None = Field(default=None, description="actor of the commit")

    @field_validator("amount_cents", mode="before")
    @classmethod
    def _check_int(cls, v: object) -> int:
        return _validate_int_cents(v)

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_iso_date(v)


class SavingsGoal(SQLModel, table=True):
    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    name: str
    target_cents: int
    target_date: str  # YYYY-MM-DD
    monthly_contribution_cents: int
    created_at: datetime = Field(default_factory=_utcnow)
    notes: str | None = None

    @field_validator("target_cents", "monthly_contribution_cents", mode="before")
    @classmethod
    def _check_positive(cls, v: object) -> int:
        if isinstance(v, bool):
            raise ValueError("must be an int, not bool")
        if isinstance(v, float):
            raise ValueError("must be an int")
        if not isinstance(v, int):
            raise ValueError("must be an int")
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("target_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return _validate_iso_date(v)


class ChatMessage(SQLModel, table=True):
    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        return v


class Rule(SQLModel, table=True):
    model_config = ConfigDict(validate_assignment=True)
    id: int | None = Field(default=None, primary_key=True)
    name: str
    payee_contains: str | None = None
    amount_min: int | None = None  # cents, inclusive
    amount_max: int | None = None  # cents, inclusive
    notes_contains: str | None = None
    category_id: int = Field(foreign_key="category.id")
    priority: int = Field(default=0)  # higher applies first
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()

    @field_validator("amount_min", "amount_max", mode="before")
    @classmethod
    def _check_amounts(cls, v: object) -> int | None:
        if v is None:
            return None
        if isinstance(v, (bool, float)):
            raise ValueError("amounts must be int cents or None")
        if not isinstance(v, int):
            raise ValueError("amounts must be int cents or None")
        if v < 0:
            raise ValueError("amounts must be non-negative")
        return v


# Re-export Tax domain models (TAX-04) so they register on SQLModel.metadata
# Re-export TAX-06 domain models so they register on SQLModel.metadata
from local_ledger.tax.classification import (  # noqa: E402,F401
    ClassificationState,
    ClassificationSubject,
    DecisionHead,
    Measure,
    TaxClassification,
)
from local_ledger.tax.models import (  # noqa: E402,F401
    AssetDepreciation,
    DocumentType,
    EvidencePacket,
    EvidenceStatus,
    ExtractedField,
    FilingStatus,
    HoldingTerm,
    InvestmentSaleRecord,
    Jurisdiction,
    JurisdictionCarryover,
    ReconciliationItem,
    ReconciliationStatus,
    RentalExpenseRecord,
    ResidencyStatus,
    ScanState,
    TaxDocument,
    TaxProperty,
    TaxQuestion,
    TaxWorkspace,
    WorkspaceStatus,
)
from local_ledger.tax.reconciliation import (  # noqa: E402,F401
    ReconciliationRun,
    RunState,
    TotalBasis,
)
from local_ledger.tax.scope import ReviewScope, ReviewScopeMode  # noqa: E402,F401
