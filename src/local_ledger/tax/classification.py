"""Tax classification: which treatment a ledger row or document line carries.

This module owns two things that the persistence layer deliberately did not
model, because they are a *judgement* rather than a fact found on a form:

- the registered treatment vocabulary (what "deductible repair expense" means
  here), and
- the record of the fact and authority that supports each classification.

Two rules from the contract are load-bearing here:

1. **A budget category is not a tax treatment.** ``RentalExpenseRecord.category``
   is a user label from a statement. It is an input to a classification, never
   the answer: the treatment is stored separately with the fact that supports
   it.
2. **Unresolved is a real answer.** A ledger row that matches no rule is
   ``UNRESOLVED`` and raises a question. It is never silently dropped (the
   amount disappears) and never silently included (it inflates a total). Those
   are different mistakes with different fixes, and the caller has to know which
   one happened.

Nothing in this module calls a model, and nothing computes tax. It records
treatment; TaxAct computes the return.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Field, Session, SQLModel, select

from local_ledger.models import Transaction

ID_PREFIX = "cls-"


class TaxTreatment(StrEnum):
    """Registered tax treatments. Adding a value is a contract change.

    The vocabulary is closed on purpose: an unregistered code cannot be stored,
    so a model or a rule cannot invent a treatment.
    """

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
    CLASSIFIED = "CLASSIFIED"
    UNRESOLVED = "UNRESOLVED"
    NEEDS_FACT = "NEEDS_FACT"


class ClassificationSubject(StrEnum):
    TRANSACTION = "TRANSACTION"
    RENTAL_EXPENSE = "RENTAL_EXPENSE"
    INVESTMENT_SALE = "INVESTMENT_SALE"


#: Treatments that mean money moving between the user's own accounts. They are
#: never income and never an expense, and they are the one exclusion the code
#: determines structurally rather than by judgement.
TRANSFER_TREATMENTS = frozenset({TaxTreatment.TRANSFER_IN, TaxTreatment.TRANSFER_OUT})

#: Treatments excluded from taxable income by nature, not by decision.
INCOME_EXCLUDED_TREATMENTS = TRANSFER_TREATMENTS | frozenset(
    {TaxTreatment.REFUND, TaxTreatment.PERSONAL, TaxTreatment.OUT_OF_SCOPE}
)

#: Treatments whose deductibility is a legal determination, so a classification
#: using one must cite authority. Compare the expense list in the fixture set:
#: cleaning, insurance and management are ordinary expenses, but repairs vs
#: improvement, principal vs interest, and depreciation all turn on a test.
AUTHORITY_REQUIRED = frozenset(
    {
        TaxTreatment.RENTAL_REPAIRS,
        TaxTreatment.CAPITAL_IMPROVEMENT,
        TaxTreatment.MORTGAGE_PRINCIPAL,
        TaxTreatment.RENTAL_MORTGAGE_INTEREST,
        TaxTreatment.DEPRECIATION,
    }
)

#: Statement category label -> treatment. A *starting point* for the reviewed
#: classification, not the classification itself: the reviewed record is what
#: counts, and this mapping is intentionally conservative. Anything not listed
#: stays unresolved rather than being guessed.
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
    # The rental fixture distinguishes interest reported on Form 1098 from any
    # other interest line, and names both. Both are mortgage interest for
    # Schedule E purposes, but keeping the two keys separate means a reviewer
    # can see which evidence the treatment rests on.
    "mortgage_interest_1098_box1": TaxTreatment.RENTAL_MORTGAGE_INTEREST,
}


def _hint_for(category: str) -> TaxTreatment | None:
    """Look a statement category up, tolerating the fixture naming convention.

    Fixture and bank categories are inconsistently suffixed (``property_taxes``
    vs ``property_taxes_cents``), so the suffix is stripped before matching
    rather than duplicating every entry.
    """
    key = category.strip().lower()
    if key in CATEGORY_HINTS:
        return CATEGORY_HINTS[key]
    stripped = key.removesuffix("_cents")
    return CATEGORY_HINTS.get(stripped)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def allocate_cents(total_cents: int, basis_points: list[int]) -> list[int]:
    """Split ``total_cents`` by basis-point shares, exactly.

    Proportional division of an amount that is not divisible by 10000 leaves a
    remainder. The remainder goes to the largest share, so the parts always sum
    back to the original - including for negative totals, where naive integer
    division rounds towards minus infinity and would leak a cent.
    """
    if sum(basis_points) != 10000:
        raise ClassificationError(
            f"shares must total 10000 basis points, got {sum(basis_points)}"
        )
    amounts = [total_cents * bp // 10000 for bp in basis_points]
    remainder = total_cents - sum(amounts)
    if remainder:
        # Give the leftover cents to the largest shares, one each, so a
        # near-even split does not push the whole remainder onto one row.
        order = sorted(range(len(basis_points)), key=lambda i: -basis_points[i])
        for i in order:
            step = 1 if remainder > 0 else -1
            amounts[i] += step
            remainder -= step
            if not remainder:
                break
    return amounts


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classifying one subject, for callers that want to show it."""

    treatment: str | None
    state: str
    reason: str


class TaxClassification(SQLModel, table=True):
    """A reviewed (or explicitly unresolved) tax treatment for one subject.

    A subject may have several rows: that is how a split is represented. Each
    row carries ``allocation_bp``, and the shares for a subject must sum to
    10000, so one ledger row can never contribute to two totals in full.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"{ID_PREFIX}{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(index=True)
    subject_type: str = Field(index=True)
    subject_id: str = Field(index=True)
    property_id: str | None = Field(default=None, index=True)
    treatment: str | None = Field(default=None, index=True)
    state: str = Field(default=ClassificationState.UNRESOLVED, index=True)
    amount_cents: int = 0
    allocation_bp: int = 10000
    fact: str = ""
    authority_citation: str | None = Field(default=None)
    reviewed_by: str | None = Field(default=None)
    reviewed_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class ClassificationError(Exception):
    """A classification was refused: unknown treatment, bad split, bad scope."""


class TaxClassificationService:
    """Records tax treatments and the fact that supports each one."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    # ---- reads ----

    def for_subject(self, subject_type: str, subject_id: str) -> list[TaxClassification]:
        stmt = select(TaxClassification).where(
            TaxClassification.workspace_id == self.workspace_id,
            TaxClassification.subject_type == subject_type,
            TaxClassification.subject_id == subject_id,
        )
        return list(self.session.exec(stmt).all())

    def classified(
        self,
        *,
        property_id: str | None = None,
        states: tuple[str, ...] = (ClassificationState.CLASSIFIED,),
    ) -> list[TaxClassification]:
        stmt = select(TaxClassification).where(
            TaxClassification.workspace_id == self.workspace_id,
            TaxClassification.state.in_(list(states)),
        )
        if property_id is not None:
            stmt = stmt.where(TaxClassification.property_id == property_id)
        return list(self.session.exec(stmt).all())

    def all_records(self) -> list[TaxClassification]:
        """Every classification for the workspace, whatever its state."""
        return list(
            self.session.exec(
                select(TaxClassification).where(
                    TaxClassification.workspace_id == self.workspace_id
                )
            ).all()
        )

    def unresolved(self) -> list[TaxClassification]:
        """Subjects with no decided treatment. Excluded from every total."""
        return [
            r
            for r in self.all_records()
            if r.state in (ClassificationState.UNRESOLVED, ClassificationState.NEEDS_FACT)
        ]

    # ---- writes ----

    def classify(
        self,
        *,
        subject_type: str,
        subject_id: str,
        amount_cents: int,
        treatment: str,
        fact: str,
        property_id: str | None = None,
        authority_citation: str | None = None,
        reviewed_by: str = "user",
        allocation_bp: int = 10000,
    ) -> TaxClassification:
        """Record a reviewed treatment for one subject.

        ``fact`` is required. "It is a repair" is a fact; "probably deductible"
        is not. A treatment in :data:`AUTHORITY_REQUIRED` additionally requires
        ``authority_citation``, because those decisions are legal tests rather
        than bookkeeping.
        """
        if not fact.strip():
            raise ClassificationError(
                "a classification needs a recorded fact; an unexplained treatment is not reviewable"
            )
        code = TaxTreatment(treatment)  # raises ValueError for an unregistered code
        if code in AUTHORITY_REQUIRED and not (authority_citation or "").strip():
            raise ClassificationError(
                f"treatment {code} turns on a legal test and requires an authority citation"
            )
        if not 0 < allocation_bp <= 10000:
            raise ClassificationError("allocation_bp must be between 1 and 10000")

        record = TaxClassification(
            workspace_id=self.workspace_id,
            subject_type=subject_type,
            subject_id=subject_id,
            property_id=property_id,
            treatment=code,
            state=ClassificationState.CLASSIFIED,
            amount_cents=amount_cents,
            allocation_bp=allocation_bp,
            fact=fact,
            authority_citation=authority_citation or None,
            reviewed_by=reviewed_by,
            reviewed_at=_utcnow(),
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    def split(
        self,
        *,
        subject_type: str,
        subject_id: str,
        total_cents: int,
        parts: list[dict],
        reviewed_by: str = "user",
    ) -> list[TaxClassification]:
        """Split one subject across several treatments or properties.

        The shares must total exactly 10000 basis points, and the allocated
        amounts must total exactly ``total_cents``. Integer division alone does
        not achieve that - a 73/27 split of an odd amount leaves a cent
        unallocated - so the remainder is given to the largest share and the
        result is asserted. A split that does not add up is refused rather than
        rounded away, because a silent gap is how a total drifts from the
        evidence that produced it.
        """
        if not parts:
            raise ClassificationError("a split needs at least one part")
        # allocate_cents refuses shares that do not total 10000, so it is the
        # single validation point; this call also proves the shares divide the
        # amount exactly. Each row stores the subject's full amount, because the
        # share is re-derived from allocation_bp wherever a total is computed -
        # storing a pre-divided amount would divide it twice.
        allocate_cents(int(total_cents), [int(p["allocation_bp"]) for p in parts])

        # Replace any previous split for this subject so re-splitting is not a
        # double count.
        previous = self.session.exec(
            select(TaxClassification).where(
                TaxClassification.workspace_id == self.workspace_id,
                TaxClassification.subject_type == subject_type,
                TaxClassification.subject_id == subject_id,
            )
        ).all()
        for record in previous:
            self.session.delete(record)
        self.session.commit()

        created: list[TaxClassification] = []
        for part in parts:
            created.append(
                self.classify(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    amount_cents=int(total_cents),
                    treatment=part["treatment"],
                    fact=part["fact"],
                    property_id=part.get("property_id"),
                    authority_citation=part.get("authority_citation"),
                    reviewed_by=reviewed_by,
                    allocation_bp=int(part["allocation_bp"]),
                )
            )
        return created

    def mark_unresolved(
        self,
        *,
        subject_type: str,
        subject_id: str,
        amount_cents: int,
        reason: str,
        property_id: str | None = None,
    ) -> TaxClassification:
        """Record that a subject has no decidable treatment yet.

        Used for anything the rules do not recognise, so the amount is visibly
        held back instead of quietly inflating a total.
        """
        record = TaxClassification(
            workspace_id=self.workspace_id,
            subject_type=subject_type,
            subject_id=subject_id,
            property_id=property_id,
            treatment=None,
            state=ClassificationState.UNRESOLVED,
            amount_cents=amount_cents,
            allocation_bp=10000,
            fact=reason,
        )
        self.session.add(record)
        self.session.commit()
        self.session.refresh(record)
        return record

    # ---- deterministic proposals ----

    def propose_for_transaction(self, transaction: Transaction) -> ClassificationResult:
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

    def propose_for_rental_expense(self, expense: object) -> ClassificationResult:
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

    def allocations_for(
        self, subject_type: str, subject_id: str
    ) -> dict[str, int]:
        """Exact per-row amounts for every classification of one subject.

        Shares are only meaningful as a set: 7321 bp of a subject means 73.21%
        *of that subject*, and the leftover cent belongs to whichever row the
        group distribution gives it to. Computing a single row's share in
        isolation would hand that row the whole remainder, so this walks the
        group and returns a record id -> amount mapping. Rows of a subject
        always sum to the subject's amount.
        """
        rows = self.for_subject(subject_type, subject_id)
        if not rows:
            return {}
        shares = [int(r.allocation_bp) for r in rows]
        total = int(rows[0].amount_cents)
        if sum(shares) != 10000:
            # A hand-edited or partially written split. Fall back to each row's
            # own share rather than inventing a distribution.
            return {
                r.id: allocate_cents(total, [int(r.allocation_bp)])[0] if r.allocation_bp == 10000 else 0
                for r in rows
            }
        amounts = allocate_cents(total, shares)
        return {r.id: amount for r, amount in zip(rows, amounts, strict=True)}

    def allocation_cents(self, record: TaxClassification) -> int:
        """The amount one classification contributes to a total.

        Uses the group distribution so that the rows of a split sum back to the
        subject's amount, including for negative totals where naive integer
        division rounds towards minus infinity and sheds a cent.
        """
        shares = self.allocations_for(record.subject_type, record.subject_id)
        return shares.get(record.id, 0)

    def as_json(self, record: TaxClassification) -> dict:
        return json.loads(record.model_dump_json(exclude={"workspace_id"}))


def subject_key(subject_type: str, subject_id: str) -> str:
    return f"{subject_type}:{subject_id}"
