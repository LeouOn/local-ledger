"""Tax classification: a reviewed treatment, owned as a versioned decision.

The hardening pass changed this module's central idea. A classification used to
be "insert a row stating a treatment", which meant it trusted the caller for the
amount, could be inserted twice, and on a failed replacement destroyed the
decision it was replacing. Instead a classification is now:

- **bound to a real source row** - the amount is read from the subject, not
  accepted from the caller, and the reviewed fingerprint of that source is stored
  alongside the decision;
- **grouped into one decision head per (workspace, subject, measure)**, holding
  all allocation parts of one revision, with the previous revision retained as
  history;
- **replay-safe** - re-submitting the identical current decision returns it
  unchanged, while a changed decision supersedes the previous revision rather
  than adding a second active amount;
- **atomic** - a split validates every part before it writes anything, and
  commits once, so a failure leaves the previous decision intact.

Two rules from the contract are still load-bearing: a budget category is not a
tax treatment, and unresolved is a real answer that appears separately rather
than being dropped or folded in. Nothing here computes tax, and nothing here
calls a model.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from typing import ParamSpec, TypeVar

from pydantic import ConfigDict
from sqlalchemy import UniqueConstraint, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, select

# Transaction is imported lazily inside resolve_subject: this module is
# registered from local_ledger/models.py, so a module-level import back into it
# is a cycle whenever this module is imported first.
from local_ledger.tax.models import (
    EvidenceStatus,
    InvestmentSaleRecord,
    RentalExpenseRecord,
    TaxProperty,
    TaxWorkspace,
    WorkspaceStatus,
)

ID_PREFIX = "cls-"


class TaxTreatment(StrEnum):
    """Registered tax treatments. Adding a value is a contract change."""

    # --- income ---
    RENTAL_GROSS_RENT = "RENTAL_GROSS_RENT"
    INVESTMENT_INTEREST = "INVESTMENT_INTEREST"
    INVESTMENT_DIVIDEND = "INVESTMENT_DIVIDEND"
    INVESTMENT_CAPITAL_GAIN_DIST = "INVESTMENT_CAPITAL_GAIN_DIST"
    INVESTMENT_PROCEEDS = "INVESTMENT_PROCEEDS"
    INVESTMENT_BASIS = "INVESTMENT_BASIS"

    # --- rental expenses, deductible on the rental schedule ---
    RENTAL_MORTGAGE_INTEREST = "RENTAL_MORTGAGE_INTEREST"
    RENTAL_PROPERTY_TAX = "RENTAL_PROPERTY_TAX"
    RENTAL_INSURANCE = "RENTAL_INSURANCE"
    RENTAL_REPAIRS = "RENTAL_REPAIRS"
    RENTAL_MANAGEMENT_FEES = "RENTAL_MANAGEMENT_FEES"
    RENTAL_UTILITIES = "RENTAL_UTILITIES"
    RENTAL_CLEANING_MAINTENANCE = "RENTAL_CLEANING_MAINTENANCE"
    RENTAL_LEGAL_PROFESSIONAL = "RENTAL_LEGAL_PROFESSIONAL"
    RENTAL_SUPPLIES = "RENTAL_SUPPLIES"
    RENTAL_ADVERTISING = "RENTAL_ADVERTISING"
    RENTAL_HOA_DUES = "RENTAL_HOA_DUES"
    RENTAL_ESCROW_DISBURSEMENT = "RENTAL_ESCROW_DISBURSEMENT"

    # --- known non-expenses, tracked so they are visibly not expenses ---
    MORTGAGE_PRINCIPAL = "MORTGAGE_PRINCIPAL"
    CAPITAL_IMPROVEMENT = "CAPITAL_IMPROVEMENT"
    DEPRECIATION = "DEPRECIATION"

    # --- excluded from taxable income ---
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    REFUND = "REFUND"
    PERSONAL = "PERSONAL"

    # --- explicitly outside the confirmed scope ---
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ClassificationState(StrEnum):
    #: A reviewed decision. Only these rows, in the current revision, contribute.
    CLASSIFIED = "CLASSIFIED"
    #: No decidable treatment yet, or a source changed and it needs review again.
    UNRESOLVED = "UNRESOLVED"
    NEEDS_FACT = "NEEDS_FACT"
    #: Replaced by a later revision. Retained, never counted.
    SUPERSEDED = "SUPERSEDED"


class ClassificationSubject(StrEnum):
    TRANSACTION = "TRANSACTION"
    RENTAL_EXPENSE = "RENTAL_EXPENSE"
    INVESTMENT_SALE = "INVESTMENT_SALE"


class Measure(StrEnum):
    """Which quantity of a subject a decision is about.

    A transaction or a rental expense has one meaningful amount, so they use
    ``AMOUNT``. An investment sale has several that must never be conflated:
    proceeds are not basis, and a missing basis stays unknown rather than being
    filled in from proceeds.
    """

    AMOUNT = "AMOUNT"
    PROCEEDS = "PROCEEDS"
    BASIS = "BASIS"
    ADJUSTMENT = "ADJUSTMENT"
    GAIN_LOSS = "GAIN_LOSS"


#: Measures that only exist on an investment sale.
SALE_MEASURES = {
    Measure.PROCEEDS: "proceeds_cents",
    Measure.BASIS: "cost_basis_cents",
    Measure.ADJUSTMENT: "wash_sale_disallowed_cents",
    Measure.GAIN_LOSS: "gain_loss_cents",
}

#: Measures a subject type may be classified against.
ALLOWED_MEASURES: dict[str, set[Measure]] = {
    ClassificationSubject.TRANSACTION: {Measure.AMOUNT},
    ClassificationSubject.RENTAL_EXPENSE: {Measure.AMOUNT},
    ClassificationSubject.INVESTMENT_SALE: set(SALE_MEASURES),
}

#: Treatments whose deductibility is a legal determination, so a classification
#: using one must cite authority.
AUTHORITY_REQUIRED = frozenset(
    {
        TaxTreatment.RENTAL_REPAIRS,
        TaxTreatment.CAPITAL_IMPROVEMENT,
        TaxTreatment.MORTGAGE_PRINCIPAL,
        TaxTreatment.RENTAL_MORTGAGE_INTEREST,
        TaxTreatment.DEPRECIATION,
    }
)

#: Treatments excluded from taxable income by nature, not by decision.
INCOME_EXCLUDED_TREATMENTS = frozenset(
    {
        str(TaxTreatment.TRANSFER_IN),
        str(TaxTreatment.TRANSFER_OUT),
        str(TaxTreatment.REFUND),
        str(TaxTreatment.PERSONAL),
        str(TaxTreatment.OUT_OF_SCOPE),
    }
)

CATEGORY_HINTS: dict[str, TaxTreatment] = {
    "mortgage_interest": TaxTreatment.RENTAL_MORTGAGE_INTEREST,
    "property_taxes": TaxTreatment.RENTAL_PROPERTY_TAX,
    "property_tax": TaxTreatment.RENTAL_PROPERTY_TAX,
    "insurance": TaxTreatment.RENTAL_INSURANCE,
    "repairs": TaxTreatment.RENTAL_REPAIRS,
    "management_fees": TaxTreatment.RENTAL_MANAGEMENT_FEES,
    "utilities": TaxTreatment.RENTAL_UTILITIES,
    "cleaning_and_maintenance": TaxTreatment.RENTAL_CLEANING_MAINTENANCE,
    "legal_and_professional": TaxTreatment.RENTAL_LEGAL_PROFESSIONAL,
    "supplies": TaxTreatment.RENTAL_SUPPLIES,
    "advertising": TaxTreatment.RENTAL_ADVERTISING,
    "hoa_dues": TaxTreatment.RENTAL_HOA_DUES,
    "principal": TaxTreatment.MORTGAGE_PRINCIPAL,
    "depreciation": TaxTreatment.DEPRECIATION,
    "mortgage_interest_1098_box1": TaxTreatment.RENTAL_MORTGAGE_INTEREST,
}


def _hint_for(category: str) -> TaxTreatment | None:
    """Look a statement category up, tolerating the fixture naming convention."""
    key = category.strip().lower()
    if key in CATEGORY_HINTS:
        return CATEGORY_HINTS[key]
    return CATEGORY_HINTS.get(key.removesuffix("_cents"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def allocate_cents(total_cents: int, basis_points: list[int]) -> list[int]:
    """Split ``total_cents`` by basis-point shares, exactly.

    Proportional division of an amount not divisible by 10000 leaves a
    remainder, and Python's ``//`` rounds towards minus infinity, so a naive
    split of a negative total would shed a cent. The remainder cents go to the
    largest shares one each, so the parts always sum back to the original.
    """
    if sum(basis_points) != 10000:
        raise ClassificationError(
            f"shares must total 10000 basis points, got {sum(basis_points)}"
        )
    amounts = [total_cents * bp // 10000 for bp in basis_points]
    remainder = total_cents - sum(amounts)
    while remainder:
        order = sorted(range(len(basis_points)), key=lambda i: -basis_points[i])
        for i in order:
            step = 1 if remainder > 0 else -1
            amounts[i] += step
            remainder -= step
            if not remainder:
                break
    return amounts


class ClassificationError(Exception):
    """A classification was refused: bad subject, treatment, split or state."""


class ReviewConflictError(ClassificationError):
    """The caller's request is based on a decision or source that has moved on.

    Raised rather than silently applying an older review over a newer one.
    """


class SourceChangedError(ClassificationError):
    """The reviewed source no longer matches the caller's expectation."""


@dataclass(frozen=True)
class ResolvedSubject:
    """A source row, its amount for a measure, and a fingerprint of it."""

    subject_type: str
    subject_id: str
    amount_cents: int | None
    fingerprint: str
    is_verified: bool
    property_id: str | None = None


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of a deterministic proposal, for callers that want to show it."""

    treatment: str | None
    state: str
    reason: str


class DecisionHead(SQLModel, table=True):
    """One decision head per (workspace, subject, measure).

    The unique constraint is the concurrency guard: two sessions cannot both own
    the current revision of the same decision, and ``version`` is the
    compare-and-set token a caller must present to replace it.
    """

    model_config = ConfigDict(validate_assignment=True)
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "subject_type",
            "subject_id",
            "measure",
            name="uq_tax_decision_head",
        ),
    )

    id: str = Field(default_factory=lambda: f"hd-{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(index=True)
    subject_type: str = Field(index=True)
    subject_id: str = Field(index=True)
    measure: str = Field(default=Measure.AMOUNT, index=True)
    current_revision: int | None = Field(default=None)
    version: int = Field(default=1)
    updated_at: datetime = Field(default_factory=_utcnow)


class TaxClassification(SQLModel, table=True):
    """One allocation part of one decision revision.

    A subject with a split has several rows sharing ``decision_id`` and
    ``revision``; their ``allocation_bp`` shares total exactly 10000, so one
    source row can never contribute to two totals in full.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"{ID_PREFIX}{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(index=True)
    decision_id: str | None = Field(default=None, index=True)
    revision: int | None = Field(default=None, index=True)
    subject_type: str = Field(index=True)
    subject_id: str = Field(index=True)
    measure: str = Field(default=Measure.AMOUNT, index=True)
    property_id: str | None = Field(default=None, index=True)
    treatment: str | None = Field(default=None, index=True)
    state: str = Field(default=ClassificationState.UNRESOLVED, index=True)
    amount_cents: int = 0
    #: Snapshot of the source amount as reviewed, so a later change is detectable.
    source_amount_cents: int | None = Field(default=None)
    source_fingerprint: str | None = Field(default=None, index=True)
    allocation_bp: int = 10000
    fact: str = ""
    authority_citation: str | None = Field(default=None)
    reviewed_by: str | None = Field(default=None)
    reviewed_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


def fingerprint(*parts: object) -> str:
    """A deterministic fingerprint of the fields a review actually depends on."""
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _atomic_review(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """A failed review rolls back its head, parts and invalidation together."""
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except Exception:
            args[0].session.rollback()
            raise
    return wrapped


class TaxClassificationService:
    """Records reviewed tax treatments as versioned, source-bound decisions."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.workspace = session.get(TaxWorkspace, workspace_id)
        if self.workspace is None:
            raise ClassificationError(f"workspace {workspace_id} not found")

    # ---- guards ----

    def _check_writable(self) -> None:
        """Every write path goes through here, so a new method cannot forget.

        A locked workspace is the user's own 'I am done editing' signal; writing
        to it behind their back would undermine every snapshot taken from it.
        """
        self.session.refresh(self.workspace)
        if self.workspace.status == WorkspaceStatus.LOCKED:
            from local_ledger.tax.service import WorkspaceLockedError

            raise WorkspaceLockedError(
                f"Workspace for year {self.workspace.tax_year} "
                f"({self.workspace.jurisdiction}) is LOCKED and immutable"
            )

    # ---- subject resolution ----

    def resolve_subject(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> ResolvedSubject:
        """Read the subject's real amount and fingerprint it, or raise.

        The amount is never taken from the caller: a classification that does not
        match its source row is a claim, not a record.
        """
        subject = ClassificationSubject(subject_type)
        if subject is ClassificationSubject.RENTAL_EXPENSE:
            record = self.session.get(RentalExpenseRecord, subject_id, populate_existing=True)
            if record is None:
                raise ClassificationError(f"rental expense {subject_id} not found")
            if record.workspace_id != self.workspace_id:
                raise ClassificationError(
                    f"rental expense {subject_id} belongs to another workspace"
                )
            if record.status != EvidenceStatus.VERIFIED:
                # Classifying an unverified amount would put unverified evidence
                # into a total; the caller must verify the source first.
                raise ClassificationError(
                    f"rental expense {subject_id} is {record.status}; verify the source "
                    "amount before classifying its treatment"
                )
            self._require_property_in_workspace(record.property_id)
            return ResolvedSubject(
                subject_type=subject_type,
                subject_id=subject_id,
                amount_cents=record.amount_cents,
                fingerprint=fingerprint(
                    record.id, record.amount_cents, record.status, record.source_document_id
                ),
                is_verified=True,
                property_id=record.property_id,
            )

        if subject is ClassificationSubject.INVESTMENT_SALE:
            record = self.session.get(InvestmentSaleRecord, subject_id, populate_existing=True)
            if record is None:
                raise ClassificationError(f"investment sale {subject_id} not found")
            if record.workspace_id != self.workspace_id:
                raise ClassificationError(
                    f"investment sale {subject_id} belongs to another workspace"
                )
            if record.status != EvidenceStatus.VERIFIED:
                raise ClassificationError(
                    f"investment sale {subject_id} is {record.status}; verify the source "
                    "amount before classifying its treatment"
                )
            attribute = SALE_MEASURES.get(Measure(measure))
            if attribute is None:
                raise ClassificationError(
                    f"measure {measure!r} is not valid for an investment sale"
                )
            return ResolvedSubject(
                subject_type=subject_type,
                subject_id=subject_id,
                amount_cents=getattr(record, attribute, None),
                fingerprint=fingerprint(
                    record.id,
                    record.status,
                    record.proceeds_cents,
                    record.cost_basis_cents,
                    record.wash_sale_disallowed_cents,
                    record.gain_loss_cents,
                ),
                is_verified=True,
            )

        # TRANSACTION
        from local_ledger.models import Transaction

        try:
            numeric = int(subject_id)
        except (TypeError, ValueError) as exc:
            raise ClassificationError(f"{subject_id!r} is not a transaction id") from exc
        record = self.session.get(Transaction, numeric, populate_existing=True)
        if record is None:
            raise ClassificationError(f"transaction {subject_id} not found")
        if not record.date.startswith(f"{self.workspace.tax_year}-"):
            raise ClassificationError(
                f"transaction {subject_id} is dated {record.date}, outside tax year "
                f"{self.workspace.tax_year}"
            )
        from local_ledger.tax.scope import WorkspaceScopeService

        scope = WorkspaceScopeService(self.session).get(self.workspace_id)
        if scope.is_configured and not scope.covers_account(record.account_id):
            raise ClassificationError(f"transaction {subject_id} is outside the review scope")
        return ResolvedSubject(
            subject_type=subject_type,
            subject_id=str(numeric),
            amount_cents=record.amount_cents,
            fingerprint=fingerprint(record.id, record.amount_cents, record.date, record.payee_id),
            is_verified=True,
        )

    def _require_property_in_workspace(self, property_id: str | None) -> None:
        if property_id is None:
            return
        prop = self.session.get(TaxProperty, property_id)
        if prop is None:
            raise ClassificationError(f"property {property_id} not found")
        if prop.workspace_id != self.workspace_id:
            raise ClassificationError(
                f"property {property_id} belongs to another workspace; an allocation cannot "
                "span workspaces or tax years"
            )

        from local_ledger.tax.scope import WorkspaceScopeService

        scope = WorkspaceScopeService(self.session).get(self.workspace_id)
        if scope.is_configured and not scope.covers_property(property_id):
            raise ClassificationError(f"property {property_id} is outside the review scope")

    # ---- deterministic proposals ----

    def propose_for_transaction(self, transaction) -> ClassificationResult:  # type: ignore[no-untyped-def]
        """Classify a ledger row from its structure alone, or decline to.

        Only transfers are determined here, because a paired transfer row is a
        structural fact and never income. Everything else is a judgement about
        money, and is left to review - guessing a treatment would be a silent
        tax decision, which is the one thing this app must never do.
        """
        if transaction.transferred_id is not None:
            direction = (
                TaxTreatment.TRANSFER_OUT
                if transaction.amount_cents < 0
                else TaxTreatment.TRANSFER_IN
            )
            return ClassificationResult(
                treatment=direction,
                state=ClassificationState.CLASSIFIED,
                reason=f"paired transfer row with transaction {transaction.transferred_id}",
            )
        return ClassificationResult(
            treatment=None,
            state=ClassificationState.UNRESOLVED,
            reason="no structural rule matches this transaction; a treatment must be reviewed",
        )

    def propose_for_rental_expense(self, expense) -> ClassificationResult:  # type: ignore[no-untyped-def]
        """Map a statement category label to a registered treatment, or decline.

        A capital-improvement suspect never auto-maps to repairs: that is the
        exact distinction the flag exists to force a human onto.
        """
        if getattr(expense, "is_capital_improvement_suspect", False):
            return ClassificationResult(
                treatment=TaxTreatment.CAPITAL_IMPROVEMENT,
                state=ClassificationState.NEEDS_FACT,
                reason=(
                    "flagged as a possible capital improvement; repairs vs improvement "
                    "is a legal test and must be reviewed against the authority"
                ),
            )
        category = str(getattr(expense, "category", "") or "")
        hint = _hint_for(category)
        if hint is None:
            return ClassificationResult(
                treatment=None,
                state=ClassificationState.UNRESOLVED,
                reason=f"statement category {category!r} has no registered treatment",
            )
        return ClassificationResult(
            treatment=hint,
            state=(
                ClassificationState.NEEDS_FACT
                if hint in AUTHORITY_REQUIRED
                else ClassificationState.CLASSIFIED
            ),
            reason=f"statement category {category!r} maps to {hint}",
        )

    # ---- decision heads ----

    def head_for(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> DecisionHead | None:
        return self.session.exec(
            select(DecisionHead).where(
                DecisionHead.workspace_id == self.workspace_id,
                DecisionHead.subject_type == subject_type,
                DecisionHead.subject_id == subject_id,
                DecisionHead.measure == measure,
            ).execution_options(populate_existing=True)
        ).first()

    def current_revision_parts(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> list[TaxClassification]:
        """The allocation parts of the current revision only.

        History is deliberately not returned here: a reader that summed history
        would double count every superseded decision.
        """
        head = self.head_for(subject_type, subject_id, measure)
        if head is None or head.current_revision is None:
            return []
        return list(
            self.session.exec(
                select(TaxClassification)
                .where(
                    TaxClassification.decision_id == head.id,
                    TaxClassification.revision == head.current_revision,
                    TaxClassification.state == ClassificationState.CLASSIFIED,
                )
                .order_by(TaxClassification.id)
            ).all()
        )

    def history_for(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> list[TaxClassification]:
        """Every part, current and superseded. For display, never for arithmetic."""
        return list(
            self.session.exec(
                select(TaxClassification)
                .where(
                    TaxClassification.workspace_id == self.workspace_id,
                    TaxClassification.subject_type == subject_type,
                    TaxClassification.subject_id == subject_id,
                    TaxClassification.measure == measure,
                )
                .order_by(TaxClassification.revision, TaxClassification.id)
            ).all()
        )

    def all_records(self) -> list[TaxClassification]:
        return list(
            self.session.exec(
                select(TaxClassification).where(
                    TaxClassification.workspace_id == self.workspace_id
                )
            ).all()
        )

    def unresolved_subjects(self) -> list[str]:
        """Subjects with no current decided revision, as ``subject_type:subject_id``."""
        heads = list(
            self.session.exec(
                select(DecisionHead).where(DecisionHead.workspace_id == self.workspace_id)
            ).all()
        )
        pending: list[str] = []
        for head in heads:
            if not self.current_revision_parts(
                head.subject_type, head.subject_id, head.measure
            ):
                pending.append(f"{head.subject_type}:{head.subject_id}")
        return pending

    # ---- writes ----

    def classify(
        self,
        *,
        subject_type: str,
        subject_id: str,
        treatment: str,
        fact: str,
        property_id: str | None = None,
        authority_citation: str | None = None,
        reviewed_by: str = "user",
        measure: str = Measure.AMOUNT,
        expected_head_version: int | None = None,
        expected_source_fingerprint: str | None = None,
        request_id: str | None = None,
    ) -> list[TaxClassification]:
        """Record a reviewed treatment as the single complete decision revision.

        This is the one-part form of :meth:`split`: a complete 100% decision. It
        validates before it writes, is a no-op when re-submitting the identical
        current decision, and supersedes the previous revision when the decision
        genuinely changes.
        """
        return self.split(
            subject_type=subject_type,
            subject_id=subject_id,
            parts=[
                {
                    "treatment": treatment,
                    "fact": fact,
                    "property_id": property_id,
                    "authority_citation": authority_citation,
                }
            ],
            reviewed_by=reviewed_by,
            measure=measure,
            expected_head_version=expected_head_version,
            expected_source_fingerprint=expected_source_fingerprint,
            request_id=request_id,
        )

    @_atomic_review
    def split(
        self,
        *,
        subject_type: str,
        subject_id: str,
        parts: list[dict],
        reviewed_by: str = "user",
        measure: str = Measure.AMOUNT,
        expected_head_version: int | None = None,
        expected_source_fingerprint: str | None = None,
        request_id: str | None = None,
    ) -> list[TaxClassification]:
        """Replace one subject's decision with a complete new revision, atomically.

        Validation happens before any write, so a rejected split leaves the
        previous decision completely intact; the replacement and the invalidation
        of downstream results then commit together.
        """
        self._check_writable()

        allowed = ALLOWED_MEASURES.get(subject_type)
        if allowed is None or Measure(measure) not in allowed:
            raise ClassificationError(
                f"measure {measure!r} is not valid for subject type {subject_type}"
            )
        measure_value = Measure(measure)

        # 1. Resolve the subject and its amount. Nothing below trusts the caller.
        source = self.resolve_subject(subject_type, subject_id, measure_value)
        if source.amount_cents is None:
            raise ClassificationError(
                f"{subject_type} {subject_id} has no value for measure {measure_value}; "
                "an unknown measure stays unknown and is not filled in from another"
            )
        if (
            expected_source_fingerprint is not None
            and expected_source_fingerprint != source.fingerprint
        ):
            raise SourceChangedError(
                f"the source for {subject_type} {subject_id} has changed since it was "
                "reviewed; review the new amount rather than carrying the old approval over"
            )

        if not parts:
            raise ClassificationError("a decision needs at least one part")

        # 2. Validate every part. No writes have happened yet.
        shares = [int(p.get("allocation_bp", 10000)) for p in parts]
        if sum(shares) != 10000:
            raise ClassificationError(
                f"a complete decision must total 10000 basis points, got {sum(shares)}"
            )
        normalised: list[dict] = []
        for part, share in zip(parts, shares, strict=True):
            if not 0 < share <= 10000:
                raise ClassificationError("allocation_bp must be between 1 and 10000")
            fact = str(part.get("fact", "") or "").strip()
            if not fact:
                raise ClassificationError(
                    "a classification needs a recorded fact; an unexplained treatment is "
                    "not reviewable"
                )
            code = TaxTreatment(str(part["treatment"]))
            authority = part.get("authority_citation")
            if code in AUTHORITY_REQUIRED and not str(authority or "").strip():
                raise ClassificationError(
                    f"treatment {code} turns on a legal test and requires an authority citation"
                )
            self._require_property_in_workspace(part.get("property_id"))
            normalised.append(
                {
                    "treatment": code,
                    "fact": fact,
                    "property_id": part.get("property_id"),
                    "authority_citation": str(authority).strip() if authority else None,
                    "allocation_bp": share,
                }
            )

        # Shares must also divide the source amount exactly. The amounts are not
        # stored per row: each row keeps the source amount and its share, and the
        # exact split is derived on read, so a stored half cannot drift from it.
        allocate_cents(source.amount_cents, shares)

        # 3. Replay detection against the *current* decision only. Comparing
        #    against history would let an old decision be resurrected.
        head = self.head_for(subject_type, subject_id, measure_value)
        current = self.current_revision_parts(subject_type, subject_id, measure_value)
        if (
            head is not None
            and current
            and head.current_revision is not None
            and len(current) == len(normalised)
            and _same_decision(current, normalised, source)
        ):
            if expected_head_version is not None and expected_head_version != head.version:
                raise ReviewConflictError(
                    "the decision changed while this request was in flight; reload and retry"
                )
            return current

        if (
            head is not None
            and expected_head_version is not None
            and expected_head_version != head.version
        ):
            raise ReviewConflictError(
                f"decision {head.id} is at version {head.version}, not the expected "
                f"{expected_head_version}; a newer review must not be overwritten by a "
                "stale request"
            )

        if head is None:
            if expected_head_version not in (None, 0):
                raise ReviewConflictError("the expected decision no longer exists; reload and retry")
            head = DecisionHead(
                workspace_id=self.workspace_id,
                subject_type=subject_type,
                subject_id=subject_id,
                measure=measure_value,
            )
            self.session.add(head)
            try:
                self.session.flush()
            except IntegrityError as exc:
                raise ReviewConflictError("another review created this decision; reload and retry") from exc

        revision = (head.current_revision or 0) + 1
        decision_id = head.id
        prior_revision = head.current_revision
        self._advance_head(head, revision)

        # Supersede the previous revision, keeping it as history.
        for row in self.session.exec(
            select(TaxClassification).where(
                TaxClassification.decision_id == decision_id,
                TaxClassification.revision == prior_revision,
                TaxClassification.state == ClassificationState.CLASSIFIED,
            )
        ).all():
            row.state = ClassificationState.SUPERSEDED
            self.session.add(row)

        created: list[TaxClassification] = []
        for part, share in zip(normalised, shares, strict=True):
            record = TaxClassification(
                workspace_id=self.workspace_id,
                decision_id=decision_id,
                revision=revision,
                subject_type=subject_type,
                subject_id=subject_id,
                measure=measure_value,
                property_id=part["property_id"],
                treatment=part["treatment"],
                state=ClassificationState.CLASSIFIED,
                amount_cents=source.amount_cents,
                source_amount_cents=source.amount_cents,
                source_fingerprint=source.fingerprint,
                allocation_bp=share,
                fact=part["fact"],
                authority_citation=part["authority_citation"],
                reviewed_by=reviewed_by,
                reviewed_at=_utcnow(),
            )
            self.session.add(record)
            created.append(record)

        self.session.flush()

        # A decision change invalidates results built on the old one, in the same
        # transaction as the decision itself, so a report is either computed from
        # the current decision or marked stale - never silently both.
        self._invalidate_downstream()
        self.session.commit()
        for record in created:
            self.session.refresh(record)
        self.session.refresh(head)
        return created

    def _advance_head(self, head: DecisionHead, revision: int | None) -> None:
        """Claim a head in the database before changing any of its review rows."""
        claimed = self.session.execute(
            update(DecisionHead)
            .where(DecisionHead.id == head.id, DecisionHead.version == head.version)
            .values(current_revision=revision, version=head.version + 1, updated_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ReviewConflictError("another review changed this decision; reload and retry")
        self.session.refresh(head)

    def _invalidate_downstream(self) -> None:
        """Stale this workspace's derived results, without committing.

        Imported lazily: service.py reaches back into this module's tables, so a
        module-level import would be a cycle.
        """
        from local_ledger.tax.service import TaxWorkspaceService

        TaxWorkspaceService(self.session)._invalidate_downstream(self.workspace_id)

    @_atomic_review
    def mark_unresolved(
        self,
        *,
        subject_type: str,
        subject_id: str,
        reason: str,
        expected_head_version: int | None = None,
        measure: str = Measure.AMOUNT,
        property_id: str | None = None,
    ) -> list[TaxClassification]:
        """Record that a subject has no decidable treatment, and why.

        Used for anything the rules do not recognise, so the amount is visibly
        held back rather than quietly inflating a total.
        """
        self._check_writable()
        try:
            source = self.resolve_subject(subject_type, subject_id, measure)
            amount = source.amount_cents or 0
            print_fp = source.fingerprint
        except ClassificationError:
            amount = 0
            print_fp = None
        head = self.head_for(subject_type, subject_id, measure)
        if expected_head_version is not None and expected_head_version != (head.version if head else 0):
            raise ReviewConflictError("the decision changed; reload before marking it unresolved")
        if head is None:
            head = DecisionHead(
                workspace_id=self.workspace_id, subject_type=subject_type,
                subject_id=subject_id, measure=Measure(measure),
            )
            self.session.add(head)
            try:
                self.session.flush()
            except IntegrityError as exc:
                raise ReviewConflictError("another review created this decision; reload and retry") from exc
        prior_revision = head.current_revision
        revision = (prior_revision or 0) + 1
        self._advance_head(head, revision)
        for row in self.session.exec(select(TaxClassification).where(
            TaxClassification.decision_id == head.id,
            TaxClassification.revision == prior_revision,
            TaxClassification.state != ClassificationState.SUPERSEDED,
        )).all():
            row.state = ClassificationState.SUPERSEDED
            self.session.add(row)
        record = TaxClassification(
            workspace_id=self.workspace_id,
            decision_id=head.id,
            revision=revision,
            subject_type=subject_type,
            subject_id=subject_id,
            measure=Measure(measure),
            property_id=property_id,
            treatment=None,
            state=ClassificationState.UNRESOLVED,
            amount_cents=amount,
            source_fingerprint=print_fp,
            allocation_bp=10000,
            fact=reason,
        )
        self.session.add(record)
        self._invalidate_downstream()
        self.session.commit()
        self.session.refresh(record)
        return [record]

    @_atomic_review
    def revalidate(self) -> int:
        """Move decisions whose source has changed to NEEDS_FACT.

        Reading a new amount must never carry an old approval forward. This is
        the explicit operation that records that; the readers also refuse to
        count a mismatched decision, so a caller that forgets to run this still
        cannot publish an approval for a changed amount.
        """
        self._check_writable()
        moved = 0
        for head in self.session.exec(
            select(DecisionHead).where(DecisionHead.workspace_id == self.workspace_id)
        ).all():
            parts = self.current_revision_parts(
                head.subject_type, head.subject_id, head.measure
            )
            if not parts:
                continue
            try:
                source = self.resolve_subject(
                    head.subject_type, head.subject_id, head.measure
                )
            except ClassificationError:
                self._advance_head(head, head.current_revision)
                for row in parts:
                    row.state = ClassificationState.NEEDS_FACT
                    self.session.add(row)
                    moved += 1
                continue
            if source.fingerprint != parts[0].source_fingerprint:
                self._advance_head(head, head.current_revision)
                for row in parts:
                    row.state = ClassificationState.NEEDS_FACT
                    self.session.add(row)
                    moved += 1
        if moved:
            self._invalidate_downstream()
            self.session.commit()
        return moved

    # ---- reads ----

    def for_subject(self, subject_type: str, subject_id: str) -> list[TaxClassification]:
        """Every part including history. Display only - never for arithmetic."""
        return list(
            self.session.exec(
                select(TaxClassification).where(
                    TaxClassification.workspace_id == self.workspace_id,
                    TaxClassification.subject_type == subject_type,
                    TaxClassification.subject_id == subject_id,
                )
            ).all()
        )

    def allocation_cents(self, record: TaxClassification) -> int:
        """What one part contributes: its exact share of the reviewed amount."""
        return allocate_cents(
            int(record.amount_cents), [int(record.allocation_bp)]
        )[0]

    def allocations_for(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> dict[str, int]:
        """Exact per-part amounts for the current revision of one decision."""
        parts = self.current_revision_parts(subject_type, subject_id, measure)
        if not parts:
            return {}
        total = int(parts[0].amount_cents)
        shares = [int(p.allocation_bp) for p in parts]
        amounts = allocate_cents(total, shares)
        return {p.id: amount for p, amount in zip(parts, amounts, strict=True)}


def _same_decision(
    current: list[TaxClassification], proposed: list[dict], source: ResolvedSubject
) -> bool:
    """Whether a submission is the identical current decision.

    Compares the whole decision - every part's treatment, fact, authority, share
    and property - plus the source fingerprint. Cosmetic whitespace is
    normalised; no fuzzy or semantic comparison is used to decide two rationales
    are the same.
    """

    def norm(value: str | None) -> str:
        return " ".join((value or "").split())

    def key(row: tuple[str, str, str, int, str | None]) -> tuple:
        return row

    existing = sorted(

            key(
                (
                    str(r.treatment),
                    norm(r.fact),
                    norm(r.authority_citation),
                    int(r.allocation_bp),
                    r.property_id,
                )
            )
            for r in current

    )
    incoming = sorted(
        key(
            (
                str(p["treatment"]),
                norm(p["fact"]),
                norm(p["authority_citation"]),
                int(p["allocation_bp"]),
                p["property_id"],
            )
        )
        for p in proposed
    )
    if existing != incoming:
        return False
    return all(r.source_fingerprint == source.fingerprint for r in current)
