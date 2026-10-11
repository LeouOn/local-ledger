"""Rental working papers: audit Schedule E inputs, never decide deductibility (TAX-14).

This module builds the per-property rental working paper from verified
evidence — verified ``RentalExpenseRecord`` rows, verified statement fields,
``TaxProperty`` facts, ``AssetDepreciation`` schedules and
``JurisdictionCarryover`` suspended-loss records — and compares it to the
recorded TaxAct entries. TaxAct computes the return; the reviewer decides
treatment; this service audits inputs and keeps the two roles apart.

Seven rules, each from a way a rental audit goes silently wrong:

**Bank flows are never consulted.** Ledger rows and classification decisions
are the other layer (TAX-06 reconciliation); this module does not import
them, so rent and expenses can never be derived from deposits and
withdrawals (tested, including structurally via an AST import check).

**Not-an-expense categories are separated, not summed.** Mortgage principal
builds equity and is not a Schedule E line 12 expense; security deposits are
held in trust pending treatment. Both appear on separate lines with review
questions, and are excluded from ``total_operating_expenses`` visibly.

**Escrow is a double-counting hazard.** Property taxes or insurance paid
through a lender escrow can also appear on the rental statement's expense
section; equal amounts on both sides raise an escrow-double-count question
rather than a silent sum.

**A repair that looks like an improvement is a question.** Records flagged
``is_capital_improvement_suspect`` (e.g. an HVAC replacement entered under
repairs) surface with their recorded authority citation; capitalization is
the reviewer's decision, never made here.

**Depreciation history is never inferred.** A property with no
``AssetDepreciation`` schedule raises a missing-schedule question; a
schedule whose prior federal depreciation is zero for an asset placed in
service before the tax year raises a suspect-history question. Unknown
history stays unknown - it is not zero.

**Ownership and use allocations are preserved, not applied.** Ownership
basis points and rented/personal days are carried on the paper; mixed
personal use raises an allocation question instead of silently scaling
amounts.

**Comparisons are recomputed from today's paper.** Each Schedule E figure
is matched against the current recorded TaxAct entry: matched, discrepant
(with the exact delta), source-drifted, not recorded, or orphaned. The
service is read-only and mutates nothing.

All arithmetic is integer cents in plain code; no model is called
(:func:`local_ledger.tax.reconciliation.assert_no_model_dependency`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from local_ledger.tax.models import (
    AssetDepreciation,
    EvidenceStatus,
    ExtractedField,
    JurisdictionCarryover,
    ReconciliationItem,
    ReconciliationStatus,
    RentalExpenseRecord,
    TaxDocument,
    TaxProperty,
)
from local_ledger.tax.reconciliation import assert_no_model_dependency

#: TaxAct sections this module owns.
RENTAL_TAX_SECTIONS = frozenset({"Schedule E"})

#: Comparison statuses: plain strings by design (see the pdf-worker and
#: TAX-13 work logs for the StrEnum identity trap this avoids).
MATCHED = "MATCHED"
DISCREPANCY = "DISCREPANCY"
SOURCE_DRIFT = "SOURCE_DRIFT"
NOT_RECORDED = "NOT_RECORDED"
ORPHANED = "ORPHANED"


class RentalAuditError(Exception):
    """Rental audit was refused."""


#: Categories that are structurally not Schedule E operating expenses.
#: Separated, shown, questioned - never silently summed into expenses.
_PRINCIPAL_CATEGORIES = frozenset({"mortgage principal", "principal"})
_DEPOSIT_CATEGORIES = frozenset({"deposits", "security deposits", "tenant deposits"})


def _canonical_category(category: str) -> str:
    return (category or "").strip().lower()


#: Statement-field aliases the working paper understands, in addition to
#: expense records: gross rents and the 1098 cross-check figures.
_FIELD_ALIASES: dict[str, str] = {
    alias: key
    for key, aliases in {
        "rents.gross": (
            "gross_rents",
            "gross_rents_received",
            "rents_received",
            "gross_rent_received",
            "rental_income_gross",
        ),
        "1098.box1_interest": (
            "mortgage_interest_1098_box1",
            "box1_mortgage_interest",
            "1098_box1_mortgage_interest",
        ),
        "1098.escrow_tax": (
            "escrow_tax_paid_by_lender",
            "escrow_taxes_paid",
            "1098_escrow_tax",
        ),
        "1098.escrow_insurance": (
            "escrow_insurance_paid_by_lender",
            "escrow_insurance_paid",
            "1098_escrow_insurance",
        ),
        "1098.principal_paid": (
            "principal_paid_current_year",
            "principal_paid",
            "1098_principal_paid",
        ),
    }.items()
    for alias in aliases
}


@dataclass(frozen=True)
class IncomeLine:
    key: str
    label: str
    amount_cents: int
    document_id: str
    field_id: str
    citation: str


@dataclass(frozen=True)
class ExpenseLine:
    """One verified expense (or separated not-an-expense) row."""

    expense_id: str
    category: str
    canonical_category: str
    amount_cents: int
    document_id: str | None
    citation: str
    separated: bool  # principal / deposit: shown apart, excluded from the total
    treatment_authority: str


@dataclass(frozen=True)
class AuditQuestion:
    code: str
    message: str
    citation: str
    subject: str | None = None  # expense/asset/field id the question blocks


@dataclass(frozen=True)
class DepreciationLine:
    asset_id: str
    description: str
    date_placed_in_service: str
    cost_basis_cents: int
    depreciable_basis_cents: int
    recovery_period_months: int
    prior_fed_cents: int
    prior_ca_cents: int
    current_fed_cents: int
    current_ca_cents: int


@dataclass(frozen=True)
class CarryoverLine:
    category: str
    prior_year_cents: int | None
    remaining_carryover_cents: int | None
    prior_return_reference: str


@dataclass(frozen=True)
class ComparisonRow:
    tax_section: str
    field_key: str
    working_paper_cents: int | None
    taxact_entered_cents: int | None
    delta_cents: int | None
    status: str
    note: str
    member_ids: tuple[str, ...] = ()


@dataclass
class PropertyWorkingPaper:
    """The reviewed rental working paper for one property."""

    property_id: str
    label: str
    property_type: str = ""
    ownership_basis_points: int = 10000
    days_rented: int = 365
    days_personal_use: int = 0
    date_placed_in_service: str = ""
    in_scope: bool = True

    income_lines: list[IncomeLine] = field(default_factory=list)
    expense_lines: list[ExpenseLine] = field(default_factory=list)
    depreciation: list[DepreciationLine] = field(default_factory=list)
    suspended_losses: list[CarryoverLine] = field(default_factory=list)
    questions: list[AuditQuestion] = field(default_factory=list)
    correction_notes: list[str] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def fully_checked(self) -> bool:
        """No unresolved question anywhere on this property's paper."""
        return not self.questions and self.in_scope


class RentalAuditService:
    """Read-only builder of per-property rental working papers and comparisons."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    # ------------------------------------------------------------------
    # working papers
    # ------------------------------------------------------------------

    def working_papers(self) -> list[PropertyWorkingPaper]:
        assert_no_model_dependency()
        properties = list(
            self.session.exec(
                select(TaxProperty).where(TaxProperty.workspace_id == self.workspace_id)
            ).all()
        )
        return [self.working_paper(p.id) for p in properties]

    def working_paper(self, property_id: str) -> PropertyWorkingPaper:
        """Build one property's working paper from verified evidence only."""
        assert_no_model_dependency()
        prop = self.session.get(TaxProperty, property_id)
        if prop is None or prop.workspace_id != self.workspace_id:
            raise RentalAuditError(
                f"property {property_id} is not in workspace {self.workspace_id}"
            )

        documents = {
            d.id: d
            for d in self.session.exec(
                select(TaxDocument).where(TaxDocument.workspace_id == self.workspace_id)
            ).all()
        }
        superseded_ids = {d.supersedes_doc_id for d in documents.values() if d.supersedes_doc_id}

        paper = PropertyWorkingPaper(
            property_id=prop.id,
            label=f"{prop.street_address}, {prop.city}, {prop.state} {prop.zip_code}",
            property_type=prop.property_type,
            ownership_basis_points=prop.ownership_basis_points,
            days_rented=prop.days_rented,
            days_personal_use=prop.days_personal_use,
            date_placed_in_service=prop.date_placed_in_service,
            in_scope=self._property_in_scope(prop.id),
        )

        self._property_checks(paper, prop)
        self._expenses(paper, documents, superseded_ids)
        self._income(paper, documents, superseded_ids)
        self._escrow_and_1098_checks(paper)
        self._totals(paper)
        self._depreciation(paper, prop)
        self._suspended_losses(paper, prop)
        # Lineage uses every document the property's expenses have ever
        # referenced, including superseded rows: the correction note must
        # survive the exclusion of the superseded evidence itself.
        lineage_doc_ids = {
            row.source_document_id
            for row in self.session.exec(
                select(RentalExpenseRecord).where(
                    RentalExpenseRecord.workspace_id == self.workspace_id,
                    RentalExpenseRecord.property_id == paper.property_id,
                )
            ).all()
            if row.source_document_id
        }
        self._correction_notes(paper, documents, superseded_ids, lineage_doc_ids)
        return paper

    # ------------------------------------------------------------------
    # build steps (named so each check is reviewable)
    # ------------------------------------------------------------------

    def _property_in_scope(self, property_id: str) -> bool:
        from local_ledger.tax.scope import WorkspaceScopeService

        scope = WorkspaceScopeService(self.session).get(self.workspace_id)
        if not scope.is_configured:
            return False
        return scope.covers_property(property_id)

    def _property_checks(self, paper: PropertyWorkingPaper, prop: TaxProperty) -> None:
        if not prop.date_placed_in_service:
            paper.questions.append(
                AuditQuestion(
                    code="MISSING_PLACED_IN_SERVICE",
                    message=(
                        "Placed-in-service date is not recorded. Depreciation and any "
                        "mid-year allocation depend on it; obtain the date before "
                        "entering Schedule E or depreciation figures."
                    ),
                    citation=paper.label,
                )
            )
        if prop.days_personal_use > 0:
            paper.questions.append(
                AuditQuestion(
                    code="MIXED_USE_ALLOCATION",
                    message=(
                        f"Property has {prop.days_rented} rented day(s) and "
                        f"{prop.days_personal_use} personal-use day(s). The use "
                        "allocation must be reviewed before scaling any figure; this "
                        "working paper preserves the day counts and does not apply them."
                    ),
                    citation=paper.label,
                )
            )
        if prop.ownership_basis_points != 10_000:
            paper.questions.append(
                AuditQuestion(
                    code="PARTIAL_OWNERSHIP",
                    message=(
                        f"Ownership is {prop.ownership_basis_points / 100:.2f}% "
                        "(basis points recorded); confirm co-ownership allocation "
                        "before entering proportional amounts."
                    ),
                    citation=paper.label,
                )
            )
        if not paper.in_scope:
            paper.questions.append(
                AuditQuestion(
                    code="SCOPE_PROPERTY_UNCOVERED",
                    message=(
                        "This property is outside the workspace's configured review "
                        "scope (or the scope is UNCONFIGURED); its figures cannot be "
                        "marked fully checked until the scope covers it."
                    ),
                    citation=paper.label,
                )
            )

    def _expenses(self, paper, documents, superseded_ids) -> None:  # type: ignore[no-untyped-def]
        rows = list(
            self.session.exec(
                select(RentalExpenseRecord)
                .where(
                    RentalExpenseRecord.workspace_id == self.workspace_id,
                    RentalExpenseRecord.property_id == paper.property_id,
                    RentalExpenseRecord.status == EvidenceStatus.VERIFIED,
                )
                .execution_options(populate_existing=True)
            ).all()
        )
        for row in rows:
            doc = documents.get(row.source_document_id) if row.source_document_id else None
            citation_parts = [doc.file_name if doc else "no source document"]
            if row.page_number:
                citation_parts.append(f"p.{row.page_number}")
            if row.box_or_line:
                citation_parts.append(str(row.box_or_line))
            citation = " ".join(citation_parts)
            canonical = _canonical_category(row.category)

            if row.source_document_id and row.source_document_id in superseded_ids:
                paper.questions.append(
                    AuditQuestion(
                        code="SUPERSEDED_SOURCE_STILL_VERIFIED",
                        message=(
                            f"{row.category} expense is still VERIFIED against a "
                            "document a corrected statement superseded; re-review."
                        ),
                        citation=citation,
                        subject=row.id,
                    )
                )
                continue

            separated = canonical in _PRINCIPAL_CATEGORIES or canonical in _DEPOSIT_CATEGORIES
            paper.expense_lines.append(
                ExpenseLine(
                    expense_id=row.id,
                    category=row.category,
                    canonical_category=canonical,
                    amount_cents=row.amount_cents,
                    document_id=row.source_document_id,
                    citation=citation,
                    separated=separated,
                    treatment_authority=row.treatment_authority,
                )
            )

            if canonical in _PRINCIPAL_CATEGORIES:
                paper.questions.append(
                    AuditQuestion(
                        code="MORTGAGE_PRINCIPAL_NOT_DEDUCTIBLE",
                        message=(
                            f"Mortgage principal of ${row.amount_cents / 100:,.2f} "
                            "builds equity and is not a deductible Schedule E expense "
                            "(IRS Pub 527); kept separate so it cannot enter line 12."
                        ),
                        citation=citation,
                        subject=row.id,
                    )
                )
            elif canonical in _DEPOSIT_CATEGORIES:
                paper.questions.append(
                    AuditQuestion(
                        code="DEPOSIT_TREATMENT",
                        message=(
                            f"Security deposits of ${row.amount_cents / 100:,.2f} are "
                            "held in trust: treatment depends on application to rent, "
                            "damages or return. Review before classifying."
                        ),
                        citation=citation,
                        subject=row.id,
                    )
                )
            if row.is_capital_improvement_suspect:
                paper.questions.append(
                    AuditQuestion(
                        code="CAPITAL_IMPROVEMENT_SUSPECT",
                        message=(
                            f"\"{row.category}\" of ${row.amount_cents / 100:,.2f} is "
                            "flagged as a possible capital improvement (e.g. "
                            "betterment/restoration). Capitalize and depreciate, or "
                            "document why it is a repair - this audit does not decide."
                        ),
                        citation=f"{citation} — {row.treatment_authority}",
                        subject=row.id,
                    )
                )

    def _income(self, paper, documents, superseded_ids) -> None:  # type: ignore[no-untyped-def]
        """Attribute gross-rents fields to this property via its statement documents.

        Expense records link property -> source documents; rents fields on one
        of those documents belong to this property. Fields on documents no
        property references are surfaced workspace-wide via the audit's
        unattributed-field question (raised from ``working_papers``).
        """
        doc_ids = {
            line.document_id for line in paper.expense_lines if line.document_id
        }
        rows = list(
            self.session.exec(
                select(ExtractedField).where(
                    ExtractedField.workspace_id == self.workspace_id
                ).execution_options(populate_existing=True)
            ).all()
        )
        for row in rows:
            if row.status != EvidenceStatus.VERIFIED or row.document_id in superseded_ids:
                continue
            key = _FIELD_ALIASES.get(row.field_name.lower())
            if key != "rents.gross" or row.document_id not in doc_ids:
                continue
            doc = documents.get(row.document_id)
            paper.income_lines.append(
                IncomeLine(
                    key=key,
                    label="Gross rents received",
                    amount_cents=row.amount_cents,
                    document_id=row.document_id,
                    field_id=row.id,
                    citation=(
                        f"{doc.file_name if doc else 'document'} p.{row.page_number} "
                        f"{row.box_or_line or row.field_name}"
                    ).strip(),
                )
            )

    def _1098_fields(self, doc_ids: set[str]) -> dict[str, list[ExtractedField]]:
        by_key: dict[str, list[ExtractedField]] = {}
        rows = list(
            self.session.exec(
                select(ExtractedField).where(
                    ExtractedField.workspace_id == self.workspace_id
                ).execution_options(populate_existing=True)
            ).all()
        )
        for row in rows:
            if row.status != EvidenceStatus.VERIFIED:
                continue
            key = _FIELD_ALIASES.get(row.field_name.lower())
            if key and key != "rents.gross" and row.document_id in doc_ids:
                by_key.setdefault(key, []).append(row)
        return by_key

    def _escrow_and_1098_checks(self, paper: PropertyWorkingPaper) -> None:
        doc_ids = {line.document_id for line in paper.expense_lines if line.document_id} | {
            line.document_id for line in paper.income_lines
        }
        fields1098 = self._1098_fields(doc_ids)

        def total(key: str) -> int | None:
            group = fields1098.get(key)
            return sum(f.amount_cents for f in group) if group else None

        def expense_total(canonical: str) -> int:
            return sum(
                line.amount_cents
                for line in paper.expense_lines
                if line.canonical_category == canonical and not line.separated
            )

        escrow_tax, escrow_ins = total("1098.escrow_tax"), total("1098.escrow_insurance")
        box1, principal = total("1098.box1_interest"), total("1098.principal_paid")

        # Escrow double counting: the same figure as an expense AND a lender
        # disbursement would enter twice. Equal amounts raise the question;
        # the reviewer checks the county bill / insurance invoice.
        for canonical, escrow, label in (
            ("property taxes", escrow_tax, "property taxes"),
            ("insurance", escrow_ins, "insurance"),
        ):
            on_statement = expense_total(canonical)
            if escrow is not None and on_statement == escrow and on_statement > 0:
                paper.questions.append(
                    AuditQuestion(
                        code="ESCROW_DOUBLE_COUNT",
                        message=(
                            f"{label.capitalize()} of ${on_statement / 100:,.2f} appear "
                            "both as an operating expense and as a lender escrow "
                            "disbursement on the 1098 statement. Verify against the "
                            "county/invoice bill and enter the figure once."
                        ),
                        citation=paper.label,
                    )
                )

        interest_expense = expense_total("mortgage interest") + expense_total("interest")
        if box1 is not None and interest_expense and interest_expense != box1:
            paper.questions.append(
                AuditQuestion(
                    code="MORTGAGE_INTEREST_MISMATCH",
                    message=(
                        f"Schedule E mortgage interest ${interest_expense / 100:,.2f} "
                        f"does not match 1098 box 1 ${box1 / 100:,.2f}; reconcile "
                        "statement vs lender figures before entering line 12."
                    ),
                    citation=paper.label,
                )
            )

        if principal:
            # Context line: principal is information, never an expense here.
            paper.totals["mortgage_principal_paid"] = principal

    def _totals(self, paper: PropertyWorkingPaper) -> None:
        paper.totals["rents"] = sum(line.amount_cents for line in paper.income_lines)
        paper.totals["total_operating_expenses"] = sum(
            line.amount_cents for line in paper.expense_lines if not line.separated
        )
        paper.totals["separated_not_expenses"] = sum(
            line.amount_cents for line in paper.expense_lines if line.separated
        )
        for canonical in sorted({line.canonical_category for line in paper.expense_lines}):
            paper.totals[f"expenses.{canonical}"] = sum(
                line.amount_cents
                for line in paper.expense_lines
                if line.canonical_category == canonical
            )

    def _depreciation(self, paper: PropertyWorkingPaper, prop: TaxProperty) -> None:
        assets = list(
            self.session.exec(
                select(AssetDepreciation).where(
                    AssetDepreciation.property_id == prop.id
                ).execution_options(populate_existing=True)
            ).all()
        )
        if not assets:
            paper.questions.append(
                AuditQuestion(
                    code="MISSING_ASSET_SCHEDULE",
                    message=(
                        "No depreciation schedule is recorded for this property. Prior "
                        "depreciation history must come from prior returns/working "
                        "papers - it is not inferred here, and an unknown history is "
                        "not zero."
                    ),
                    citation=paper.label,
                )
            )
            return
        tax_year = self._tax_year()
        for asset in assets:
            paper.depreciation.append(
                DepreciationLine(
                    asset_id=asset.id,
                    description=asset.description,
                    date_placed_in_service=asset.date_placed_in_service,
                    cost_basis_cents=asset.cost_basis_cents,
                    depreciable_basis_cents=asset.depreciable_basis_cents,
                    recovery_period_months=asset.recovery_period_months,
                    prior_fed_cents=asset.prior_depreciation_fed_cents,
                    prior_ca_cents=asset.prior_depreciation_ca_cents,
                    current_fed_cents=asset.current_depreciation_fed_cents,
                    current_ca_cents=asset.current_depreciation_ca_cents,
                )
            )
            in_service_year = asset.date_placed_in_service[:4]
            if (
                in_service_year.isdigit()
                and int(in_service_year) < tax_year
                and asset.prior_depreciation_fed_cents == 0
            ):
                paper.questions.append(
                    AuditQuestion(
                        code="SUSPECT_MISSING_DEPRECIATION_HISTORY",
                        message=(
                            f"Asset \"{asset.description}\" was placed in service "
                            f"{asset.date_placed_in_service} but prior federal "
                            "depreciation is recorded as zero. Missing history is not "
                            "zero; obtain the prior-return schedule or confirm no "
                            "depreciation was ever taken."
                        ),
                        citation=f"{paper.label} — {asset.description}",
                        subject=asset.id,
                    )
                )

    def _suspended_losses(self, paper: PropertyWorkingPaper, prop: TaxProperty) -> None:
        rows = list(
            self.session.exec(
                select(JurisdictionCarryover).where(
                    JurisdictionCarryover.workspace_id == self.workspace_id,
                    JurisdictionCarryover.property_id == prop.id,
                ).execution_options(populate_existing=True)
            ).all()
        )
        for row in rows:
            if row.category not in ("SUSPENDED_LOSS", "RENTAL_SUSPENDED_LOSS"):
                continue
            paper.suspended_losses.append(
                CarryoverLine(
                    category=row.category,
                    prior_year_cents=row.prior_year_cents,
                    remaining_carryover_cents=row.remaining_carryover_cents,
                    prior_return_reference=row.prior_return_reference,
                )
            )
            if row.prior_year_cents is None:
                paper.questions.append(
                    AuditQuestion(
                        code="MISSING_SUSPENDED_LOSS_HISTORY",
                        message=(
                            "Suspended passive rental loss carryover is UNKNOWN: no "
                            "prior-year figure is available. Obtain the prior return; "
                            "missing history is not zero."
                        ),
                        citation=row.prior_return_reference or paper.label,
                    )
                )

    def _correction_notes(self, paper, documents, superseded_ids, lineage_doc_ids) -> None:  # type: ignore[no-untyped-def]
        for doc_id in sorted(lineage_doc_ids & superseded_ids):
            doc = documents.get(doc_id)
            replacement = next(
                (d for d in documents.values() if d.supersedes_doc_id == doc_id), None
            )
            if doc is None:
                continue
            paper.correction_notes.append(
                f"Corrected statement: {replacement.file_name if replacement else 'replacement'} "
                f"supersedes {doc.file_name}; superseded evidence is excluded."
            )

    def _tax_year(self) -> int:
        from local_ledger.tax.models import TaxWorkspace

        ws = self.session.get(TaxWorkspace, self.workspace_id)
        return int(ws.tax_year) if ws is not None else 0

    # ------------------------------------------------------------------
    # TaxAct comparisons
    # ------------------------------------------------------------------

    def expected_comparison_keys(self, property_id: str) -> list[tuple[str, str]]:
        pairs = [
            ("Schedule E", f"{property_id}.rents"),
            ("Schedule E", f"{property_id}.total_operating_expenses"),
        ]
        paper = self.working_paper(property_id)
        for canonical in sorted({line.canonical_category for line in paper.expense_lines}):
            pairs.append(("Schedule E", f"{property_id}.expenses.{canonical}"))
        return pairs

    def comparisons(self, property_id: str | None = None) -> list[ComparisonRow]:
        """Recompute Schedule E comparisons for one property (or all) from today's papers."""
        papers = self.working_papers() if property_id is None else [self.working_paper(property_id)]

        recorded = {
            (i.tax_section, i.field_key): i
            for i in self.session.exec(
                select(ReconciliationItem)
                .where(ReconciliationItem.workspace_id == self.workspace_id)
                .execution_options(populate_existing=True)
            ).all()
            if i.status != ReconciliationStatus.STALE and i.tax_section in RENTAL_TAX_SECTIONS
        }

        rows: list[ComparisonRow] = []
        for paper in papers:
            member_by_key: dict[str, tuple[str, ...]] = {
                f"{paper.property_id}.rents": tuple(
                    line.field_id for line in paper.income_lines
                ),
            }
            for canonical in {line.canonical_category for line in paper.expense_lines}:
                member_by_key[f"{paper.property_id}.expenses.{canonical}"] = tuple(
                    line.expense_id
                    for line in paper.expense_lines
                    if line.canonical_category == canonical
                )
            member_by_key[f"{paper.property_id}.total_operating_expenses"] = tuple(
                line.expense_id for line in paper.expense_lines if not line.separated
            )

            for _section, key in self.expected_comparison_keys(paper.property_id):
                # Working-paper totals drop the property-id prefix:
                # "{prop}.expenses.repairs" -> "expenses.repairs".
                totals_key = key.split(".", 1)[1]
                expected = paper.totals.get(totals_key, 0)
                item = recorded.pop((_section, key), None)
                rows.append(
                    self._row_for(key, expected, item, member_by_key.get(key, ()))
                )

        for (_section, key), item in sorted(recorded.items()):
            rows.append(
                ComparisonRow(
                    tax_section=_section,
                    field_key=key,
                    working_paper_cents=None,
                    taxact_entered_cents=item.taxact_entered_cents,
                    delta_cents=None,
                    status=ORPHANED,
                    note=(
                        "a current comparison exists for this key but the working "
                        "paper no longer produces it; re-check the key or its evidence"
                    ),
                )
            )
        return rows

    def _row_for(self, key: str, expected: int, item, member_ids: tuple[str, ...]) -> ComparisonRow:  # type: ignore[no-untyped-def]
        if item is None:
            return ComparisonRow(
                tax_section="Schedule E",
                field_key=key,
                working_paper_cents=expected,
                taxact_entered_cents=None,
                delta_cents=None,
                status=NOT_RECORDED,
                note="no reviewed TaxAct entry recorded for this figure yet",
                member_ids=member_ids,
            )
        if item.source_working_paper_cents != expected:
            return ComparisonRow(
                tax_section="Schedule E",
                field_key=key,
                working_paper_cents=expected,
                taxact_entered_cents=item.taxact_entered_cents,
                delta_cents=None,
                status=SOURCE_DRIFT,
                note=(
                    "the working paper changed since this entry was recorded "
                    f"({item.source_working_paper_cents} then, {expected} now); "
                    "re-review and re-record"
                ),
                member_ids=member_ids,
            )
        delta = item.taxact_entered_cents - expected
        synthesized = (
            "TaxAct matches the reviewed working paper"
            if delta == 0
            else f"TaxAct differs by ${abs(delta) / 100:,.2f} "
            f"({'over' if delta > 0 else 'under'}-entered)"
        )
        return ComparisonRow(
            tax_section="Schedule E",
            field_key=key,
            working_paper_cents=expected,
            taxact_entered_cents=item.taxact_entered_cents,
            delta_cents=delta,
            status=MATCHED if delta == 0 else DISCREPANCY,
            # The reviewer's recorded explanation travels with the entry.
            note=str(item.explanation or "").strip() or synthesized,
            member_ids=member_ids,
        )
