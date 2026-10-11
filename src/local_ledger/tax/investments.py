"""Investment working papers: audit 1099 inputs, never compute the return (TAX-13).

This module builds the reviewed working paper for 1099-INT/DIV/B and
consolidated brokerage statements, and compares it to recorded TaxAct
entries. TaxAct computes the return; this service audits inputs.

Six rules, each from a way an investment audit goes silently wrong:

**Source-form evidence only.** Working-paper figures come from verified
``ExtractedField`` rows (statement boxes) and verified
``InvestmentSaleRecord`` rows (8949 lots). Bank flows are never consulted: a
brokerage withdrawal is not a sale and a deposit is not income, so this
module does not read ``Transaction`` at all - gains can never derive from
bank withdrawals/deposits because the path does not exist (tested).

**Missing basis is unknown, not zero.** A noncovered lot without a reported
basis contributes its *proceeds* (they are known) and its basis becomes an
explicit unresolved amount with a question. Summing a null basis as zero is
the one representation guaranteed to produce a wrong Schedule D.

**A number is only as good as its lineage.** Verified rows that reference a
superseded document must not exist (the service cascades them to
SUPERSEDED), but if one is ever observed it is flagged loudly rather than
summed quietly. Corrected statements surface as a lineage note with the
predecessor's exclusion stated.

**Reported figures are cross-checked, not trusted.** A covered lot's
reported gain must equal proceeds - basis; a 1099-B summary line is
cross-footed against its detail lots; qualified dividends cannot exceed
ordinary dividends; identical lots appearing on two documents are a
cross-account duplicate question. Every check raises a *question with a
citation*, never an automatic correction.

**Wash-sale adjustments are surfaced, not resolved.** The broker's
disallowed amount is preserved and the net-after-wash figure is shown next
to the gross one; the replacement-purchase review is explicitly the
reviewer's. Neither a broker statement nor a model is treated as having
resolved every adjustment.

**Comparisons are recomputed from today's paper.** A recorded TaxAct
comparison is matched against the current working-paper amount: matched,
discrepant (with the exact delta), source-drifted (the paper moved since
the entry was recorded) or not yet recorded. History stays with the
comparison records; this module is read-only and mutates nothing.

All arithmetic is integer cents in plain code; no model is called
(:func:`local_ledger.tax.reconciliation.assert_no_model_dependency`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from local_ledger.tax.models import (
    EvidenceStatus,
    ExtractedField,
    InvestmentSaleRecord,
    JurisdictionCarryover,
    ReconciliationItem,
    ReconciliationStatus,
    TaxDocument,
)
from local_ledger.tax.reconciliation import assert_no_model_dependency

#: TaxAct sections this module owns. Comparisons recorded under other
#: sections (rentals, wages) are not investment comparisons and are left to
#: their own tracks.
INVESTMENT_TAX_SECTIONS = frozenset(
    {"1099-INT", "1099-DIV", "1099-B", "Form 8949", "Schedule B", "Schedule D"}
)

SHORT_TERM = "SHORT_TERM"
LONG_TERM = "LONG_TERM"


@dataclass(frozen=True)
class IncomeFieldSpec:
    """A canonical working-paper income key and the field names it answers to."""

    key: str
    label: str
    tax_section: str
    box: str
    aliases: tuple[str, ...]


#: Canonical income keys. Aliases cover the fixture-style names
#: (``box1a_total_ordinary_dividends``) and the short manual-entry names
#: (``box1a_dividends``); matching is exact on lowercased field_name.
INCOME_FIELDS: dict[str, IncomeFieldSpec] = {
    spec.key: spec
    for spec in (
        IncomeFieldSpec(
            key="interest.box1",
            label="Interest income (1099-INT box 1)",
            tax_section="1099-INT",
            box="Box 1",
            aliases=(
                "box1_interest",
                "box1_interest_income",
                "interest_income",
                "1099_int_box1",
            ),
        ),
        IncomeFieldSpec(
            key="interest.box3",
            label="U.S. savings bonds / Treasury interest (1099-INT box 3)",
            tax_section="1099-INT",
            box="Box 3",
            aliases=(
                "box3_interest",
                "box3_us_savings_bonds",
                "box3_treasury_interest",
                "1099_int_box3",
            ),
        ),
        IncomeFieldSpec(
            key="dividends.box1a",
            label="Total ordinary dividends (1099-DIV box 1a)",
            tax_section="1099-DIV",
            box="Box 1a",
            aliases=(
                "box1a_dividends",
                "box1a_total_ordinary_dividends",
                "ordinary_dividends",
                "1099_div_box1a",
            ),
        ),
        IncomeFieldSpec(
            key="dividends.box1b",
            label="Qualified dividends (1099-DIV box 1b)",
            tax_section="1099-DIV",
            box="Box 1b",
            aliases=(
                "box1b_qualified_dividends",
                "qualified_dividends",
                "1099_div_box1b",
            ),
        ),
        IncomeFieldSpec(
            key="dividends.box2a",
            label="Total capital gain distributions (1099-DIV box 2a)",
            tax_section="1099-DIV",
            box="Box 2a",
            aliases=(
                "box2a_capital_gain_distributions",
                "box2a_total_capital_gain_distr",
                "capital_gain_distributions",
                "1099_div_box2a",
            ),
        ),
        IncomeFieldSpec(
            key="dividends.box7",
            label="Foreign tax paid (1099-DIV box 7)",
            tax_section="1099-DIV",
            box="Box 7",
            aliases=("box7_foreign_tax_paid", "foreign_tax_paid", "1099_div_box7"),
        ),
        IncomeFieldSpec(
            key="summary.gross_proceeds",
            label="1099-B summary gross proceeds (statement summary line)",
            tax_section="1099-B",
            box="Summary",
            aliases=(
                "1099b_gross_proceeds",
                "1099_b_total_proceeds",
                "total_proceeds",
                "gross_proceeds",
            ),
        ),
    )
}

_ALIASES_TO_KEY: dict[str, str] = {
    alias.lower(): spec.key for spec in INCOME_FIELDS.values() for alias in spec.aliases
}


@dataclass(frozen=True)
class IncomeLine:
    """One verified statement box, with its citation."""

    key: str
    label: str
    amount_cents: int
    document_id: str
    field_id: str
    citation: str


@dataclass(frozen=True)
class PendingIncome:
    """A proposed-but-unverified box: visible, excluded from totals."""

    field_name: str
    amount_cents: int
    citation: str


@dataclass(frozen=True)
class SaleLine:
    """One 8949 lot, exactly as evidenced, with its check outcomes."""

    sale_id: str
    description: str
    cusip_or_ticker: str | None
    date_acquired: str | None
    date_sold: str
    term: str
    covered: bool
    proceeds_cents: int
    cost_basis_cents: int | None
    gain_loss_cents: int | None
    net_after_wash_cents: int | None
    wash_sale_disallowed_cents: int
    document_id: str
    citation: str
    basis_unknown: bool


@dataclass(frozen=True)
class AuditQuestion:
    """A finding the reviewer must answer. Never an automatic correction."""

    code: str
    message: str
    citation: str


@dataclass(frozen=True)
class CarryoverLine:
    """Prior-year federal capital-loss context (deeper treatment is TAX-15)."""

    category: str
    prior_year_cents: int | None
    utilized_current_year_cents: int
    remaining_carryover_cents: int | None
    prior_return_reference: str


@dataclass(frozen=True)
class ComparisonRow:
    """Working-paper amount vs the recorded TaxAct entry for one key."""

    tax_section: str
    field_key: str
    working_paper_cents: int | None
    taxact_entered_cents: int | None
    delta_cents: int | None
    status: str  # MATCHED | DISCREPANCY | SOURCE_DRIFT | NOT_RECORDED | ORPHANED
    note: str
    member_ids: tuple[str, ...] = ()


#: Comparison statuses (strings, not an enum, so recorded items and audit
#: rows compose without identity pitfalls).
MATCHED = "MATCHED"
DISCREPANCY = "DISCREPANCY"
SOURCE_DRIFT = "SOURCE_DRIFT"
NOT_RECORDED = "NOT_RECORDED"
ORPHANED = "ORPHANED"


@dataclass
class InvestmentWorkingPaper:
    """The reviewed investment working paper for one workspace."""

    totals: dict[str, int] = field(default_factory=dict)
    income_lines: list[IncomeLine] = field(default_factory=list)
    pending_income: list[PendingIncome] = field(default_factory=list)
    unmapped_fields: list[PendingIncome] = field(default_factory=list)
    sales: list[SaleLine] = field(default_factory=list)
    carryovers: list[CarryoverLine] = field(default_factory=list)
    questions: list[AuditQuestion] = field(default_factory=list)
    correction_notes: list[str] = field(default_factory=list)

    @property
    def unresolved_basis_proceeds_cents(self) -> int:
        return self.totals.get("unknown_basis_proceeds", 0)

    @property
    def unresolved_basis_lot_count(self) -> int:
        return sum(1 for s in self.sales if s.basis_unknown)


class InvestmentAuditError(Exception):
    """Investment audit was refused."""


class InvestmentAuditService:
    """Read-only builder of the investment working paper and its comparisons."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    # ------------------------------------------------------------------
    # working paper
    # ------------------------------------------------------------------

    def working_paper(self) -> InvestmentWorkingPaper:
        """Build the working paper from verified source-form evidence only."""
        assert_no_model_dependency()

        documents = {
            d.id: d
            for d in self.session.exec(
                select(TaxDocument).where(TaxDocument.workspace_id == self.workspace_id)
            ).all()
        }
        superseded_ids = {d.supersedes_doc_id for d in documents.values() if d.supersedes_doc_id}

        fields = list(
            self.session.exec(
                select(ExtractedField).where(
                    ExtractedField.workspace_id == self.workspace_id
                ).execution_options(populate_existing=True)
            ).all()
        )
        sales = list(
            self.session.exec(
                select(InvestmentSaleRecord).where(
                    InvestmentSaleRecord.workspace_id == self.workspace_id,
                    InvestmentSaleRecord.status == EvidenceStatus.VERIFIED,
                ).execution_options(populate_existing=True)
            ).all()
        )

        paper = InvestmentWorkingPaper()
        income_by_key: dict[str, list[IncomeLine]] = {}
        for f in fields:
            doc = documents.get(f.document_id)
            citation = _field_citation(f, doc)
            if f.status == EvidenceStatus.PROPOSED:
                paper.pending_income.append(
                    PendingIncome(field_name=f.field_name, amount_cents=f.amount_cents, citation=citation)
                )
                continue
            if f.status != EvidenceStatus.VERIFIED:
                continue  # rejected/superseded history: excluded, already traceable
            if f.document_id in superseded_ids:
                # Defensive: the service cascades these to SUPERSEDED on
                # correction; if one is ever observed verified, say so loudly.
                paper.questions.append(
                    AuditQuestion(
                        code="SUPERSEDED_SOURCE_STILL_VERIFIED",
                        message=(
                            f"Field {f.field_name} is still VERIFIED against a document "
                            "that a corrected statement superseded; re-review the correction."
                        ),
                        citation=citation,
                    )
                )
                continue
            key = _ALIASES_TO_KEY.get(f.field_name.lower())
            if key is None:
                paper.unmapped_fields.append(
                    PendingIncome(field_name=f.field_name, amount_cents=f.amount_cents, citation=citation)
                )
                continue
            spec = INCOME_FIELDS[key]
            line = IncomeLine(
                key=key,
                label=spec.label,
                amount_cents=f.amount_cents,
                document_id=f.document_id,
                field_id=f.id,
                citation=citation,
            )
            income_by_key.setdefault(key, []).append(line)
            paper.income_lines.append(line)

        for line in self._sale_lines(sales, documents):
            paper.sales.append(line)

        self._income_totals(paper, income_by_key)
        self._sale_totals(paper)
        self._income_checks(paper, income_by_key)
        self._summary_checks(paper, income_by_key)
        self._cross_document_checks(paper)
        self._correction_notes(paper, documents, superseded_ids)
        self._carryovers(paper)
        return paper

    # Breaking the build into named steps keeps each check reviewable.

    def _sale_lines(self, sales, documents) -> list[SaleLine]:  # type: ignore[no-untyped-def]
        lines: list[SaleLine] = []
        for s in sales:
            doc = documents.get(s.source_document_id)
            citation = _sale_citation(s, doc)
            basis_unknown = s.cost_basis_cents is None
            # Net-after-wash exists only where the statement supplied both a
            # reported gain and a basis; anything else stays unknown rather
            # than being computed from partial figures.
            net_after_wash: int | None = None
            if s.cost_basis_cents is not None and s.gain_loss_cents is not None:
                net_after_wash = s.gain_loss_cents + s.wash_sale_disallowed_cents
            lines.append(
                SaleLine(
                    sale_id=s.id,
                    description=s.security_description,
                    cusip_or_ticker=s.cusip_or_ticker,
                    date_acquired=s.date_acquired,
                    date_sold=s.date_sold,
                    term=s.term,
                    covered=s.basis_reported_to_irs,
                    proceeds_cents=s.proceeds_cents,
                    cost_basis_cents=s.cost_basis_cents,
                    gain_loss_cents=s.gain_loss_cents,
                    net_after_wash_cents=net_after_wash,
                    wash_sale_disallowed_cents=s.wash_sale_disallowed_cents,
                    document_id=s.source_document_id,
                    citation=citation,
                    basis_unknown=basis_unknown,
                )
            )
        return lines

    def _income_totals(self, paper: InvestmentWorkingPaper, income_by_key: dict[str, list[IncomeLine]]) -> None:
        for key in sorted(income_by_key):
            paper.totals[key] = sum(line.amount_cents for line in income_by_key[key])

    def _sale_totals(self, paper: InvestmentWorkingPaper) -> None:
        short = [s for s in paper.sales if s.term == SHORT_TERM]
        long_ = [s for s in paper.sales if s.term == LONG_TERM]

        def known(lines: list[SaleLine]) -> list[SaleLine]:
            return [s for s in lines if not s.basis_unknown]

        paper.totals["proceeds_short_term"] = sum(s.proceeds_cents for s in short)
        paper.totals["proceeds_long_term"] = sum(s.proceeds_cents for s in long_)
        paper.totals["basis_reported_short_term"] = sum(
            s.cost_basis_cents or 0 for s in known(short)
        )
        paper.totals["basis_reported_long_term"] = sum(
            s.cost_basis_cents or 0 for s in known(long_)
        )
        paper.totals["wash_sale_disallowed"] = sum(
            s.wash_sale_disallowed_cents for s in paper.sales
        )
        # Gains only exist where basis is known: unknown basis is unresolved,
        # not zero, and must never masquerade as a computed gain.
        paper.totals["gain_reported_short_term"] = sum(
            s.gain_loss_cents or 0 for s in known(short) if s.gain_loss_cents is not None
        )
        paper.totals["gain_reported_long_term"] = sum(
            s.gain_loss_cents or 0 for s in known(long_) if s.gain_loss_cents is not None
        )
        paper.totals["net_gain_short_term"] = sum(
            s.net_after_wash_cents or 0 for s in known(short)
        )
        paper.totals["net_gain_long_term"] = sum(
            s.net_after_wash_cents or 0 for s in known(long_)
        )
        unknown = [s for s in paper.sales if s.basis_unknown]
        paper.totals["unknown_basis_proceeds"] = sum(s.proceeds_cents for s in unknown)

        for s in unknown:
            paper.questions.append(
                AuditQuestion(
                    code="UNKNOWN_BASIS",
                    message=(
                        f"{s.description} sold {s.date_sold}: basis is not reported "
                        f"(noncovered). Proceeds ${s.proceeds_cents / 100:,.2f} are known; "
                        "the basis is UNKNOWN, not zero. Obtain the actual basis from "
                        "records before entering the gain."
                    ),
                    citation=s.citation,
                )
            )
        for s in paper.sales:
            if s.wash_sale_disallowed_cents:
                paper.questions.append(
                    AuditQuestion(
                        code="WASH_SALE_REVIEW",
                        message=(
                            f"{s.description} reports a wash-sale loss disallowance of "
                            f"${s.wash_sale_disallowed_cents / 100:,.2f}. The replacement "
                            "purchase and its adjusted basis must be reviewed; this audit "
                            "does not resolve the adjustment."
                        ),
                        citation=s.citation,
                    )
                )
            if (
                s.cost_basis_cents is not None
                and s.gain_loss_cents is not None
                and s.gain_loss_cents != s.proceeds_cents - s.cost_basis_cents
            ):
                paper.questions.append(
                    AuditQuestion(
                        code="ARITHMETIC_MISMATCH",
                        message=(
                            f"{s.description}: reported gain "
                            f"${s.gain_loss_cents / 100:,.2f} does not equal proceeds "
                            f"${s.proceeds_cents / 100:,.2f} minus basis "
                            f"${s.cost_basis_cents / 100:,.2f}; re-check the statement entry."
                        ),
                        citation=s.citation,
                    )
                )

    def _income_checks(self, paper: InvestmentWorkingPaper, income_by_key: dict[str, list[IncomeLine]]) -> None:
        def amount(key: str) -> int:
            return paper.totals.get(key, 0)

        if amount("dividends.box1b") > amount("dividends.box1a"):
            paper.questions.append(
                AuditQuestion(
                    code="QUALIFIED_EXCEEDS_ORDINARY",
                    message=(
                        "Qualified dividends exceed total ordinary dividends "
                        f"(${amount('dividends.box1b') / 100:,.2f} vs "
                        f"${amount('dividends.box1a') / 100:,.2f}); box 1b cannot exceed box 1a."
                    ),
                    citation=_first_citation(income_by_key, "dividends.box1b"),
                )
            )
        if amount("dividends.box2a") > amount("dividends.box1a"):
            paper.questions.append(
                AuditQuestion(
                    code="CAPITAL_DISTR_EXCEEDS_ORDINARY",
                    message=(
                        "Capital gain distributions exceed total ordinary dividends "
                        f"(${amount('dividends.box2a') / 100:,.2f} vs "
                        f"${amount('dividends.box1a') / 100:,.2f}); re-check both boxes."
                    ),
                    citation=_first_citation(income_by_key, "dividends.box2a"),
                )
            )

    def _summary_checks(self, paper: InvestmentWorkingPaper, income_by_key: dict[str, list[IncomeLine]]) -> None:
        """Cross-foot 1099-B summary lines against detail lots per document."""
        summaries = income_by_key.get("summary.gross_proceeds", [])
        if not summaries:
            return
        sales_by_doc: dict[str, list[SaleLine]] = {}
        for s in paper.sales:
            sales_by_doc.setdefault(s.document_id, []).append(s)
        for summary in summaries:
            lots = sales_by_doc.get(summary.document_id, [])
            detail = sum(s.proceeds_cents for s in lots)
            dup = next(
                (s for s in lots if s.proceeds_cents == summary.amount_cents), None
            )
            if dup is not None:
                paper.questions.append(
                    AuditQuestion(
                        code="DUPLICATE_SUMMARY_DETAIL",
                        message=(
                            "The 1099-B summary gross proceeds "
                            f"(${summary.amount_cents / 100:,.2f}) equal lot "
                            f"\"{dup.description}\"'s proceeds on the same statement: "
                            "possible duplicate reporting between summary and detail. "
                            "Enter the figure once."
                        ),
                        citation=f"{summary.citation}; {dup.citation}",
                    )
                )
            elif lots and detail != summary.amount_cents:
                paper.questions.append(
                    AuditQuestion(
                        code="SUMMARY_CROSSFOOT",
                        message=(
                            "1099-B summary gross proceeds "
                            f"(${summary.amount_cents / 100:,.2f}) do not equal the sum "
                            f"of detail lots (${detail / 100:,.2f}) on the same statement; "
                            "reconcile before entering Schedule D totals."
                        ),
                        citation=summary.citation,
                    )
                )

    def _cross_document_checks(self, paper: InvestmentWorkingPaper) -> None:
        """Identical lots on two documents are a cross-account duplicate question."""
        seen: dict[tuple[str, str, int], SaleLine] = {}
        for s in sorted(paper.sales, key=lambda x: x.sale_id):
            if not s.cusip_or_ticker:
                continue
            signature = (s.cusip_or_ticker, s.date_sold, s.proceeds_cents)
            other = seen.get(signature)
            if other is not None and other.document_id != s.document_id:
                paper.questions.append(
                    AuditQuestion(
                        code="DUPLICATE_CROSS_DOCUMENT",
                        message=(
                            f"\"{s.description}\" (CUSIP {s.cusip_or_ticker}, sold "
                            f"{s.date_sold}, ${s.proceeds_cents / 100:,.2f}) appears on "
                            "two different statements; confirm whether it is a transfer "
                            "between accounts (report once) or a duplicate import."
                        ),
                        citation=f"{other.citation}; {s.citation}",
                    )
                )
            else:
                seen[signature] = s

    def _correction_notes(self, paper: InvestmentWorkingPaper, documents, superseded_ids) -> None:  # type: ignore[no-untyped-def]
        for doc_id in sorted(superseded_ids):
            doc = documents.get(doc_id)
            replacement = next(
                (d for d in documents.values() if d.supersedes_doc_id == doc_id), None
            )
            if doc is None:
                continue
            paper.correction_notes.append(
                f"Corrected statement: {replacement.file_name if replacement else 'replacement'} "
                f"supersedes {doc.file_name}; evidence from the superseded statement is "
                "excluded from the working paper."
            )

    def _carryovers(self, paper: InvestmentWorkingPaper) -> None:
        rows = list(
            self.session.exec(
                select(JurisdictionCarryover).where(
                    JurisdictionCarryover.workspace_id == self.workspace_id,
                    JurisdictionCarryover.category == "CAPITAL_LOSS",
                ).execution_options(populate_existing=True)
            ).all()
        )
        for row in rows:
            paper.carryovers.append(
                CarryoverLine(
                    category=row.category,
                    prior_year_cents=row.prior_year_cents,
                    utilized_current_year_cents=row.utilized_current_year_cents,
                    remaining_carryover_cents=row.remaining_carryover_cents,
                    prior_return_reference=row.prior_return_reference,
                )
            )
            if row.prior_year_cents is None:
                paper.questions.append(
                    AuditQuestion(
                        code="MISSING_PRIOR_RETURN",
                        message=(
                            "Federal capital-loss carryover is recorded as UNKNOWN: no "
                            "prior-year figure is available. Obtain the 2024 return "
                            "before entering the carryover; an unrecorded carryover is "
                            "missing information, not zero."
                        ),
                        citation=row.prior_return_reference or "no prior return reference recorded",
                    )
                )

    # ------------------------------------------------------------------
    # TaxAct comparisons
    # ------------------------------------------------------------------

    def expected_comparison_keys(self) -> list[tuple[str, str]]:
        """(tax_section, field_key) pairs this working paper can stand behind."""
        pairs: list[tuple[str, str]] = []
        for key, spec in INCOME_FIELDS.items():
            if key != "summary.gross_proceeds":
                pairs.append((spec.tax_section, key))
        pairs.extend(
            [
                ("Form 8949", "proceeds_short_term"),
                ("Form 8949", "proceeds_long_term"),
                ("Form 8949", "basis_reported_short_term"),
                ("Form 8949", "basis_reported_long_term"),
                ("Form 8949", "wash_sale_disallowed"),
                ("Schedule D", "gain_reported_short_term"),
                ("Schedule D", "gain_reported_long_term"),
                ("Schedule D", "net_gain_short_term"),
                ("Schedule D", "net_gain_long_term"),
            ]
        )
        return pairs

    def comparisons(self) -> list[ComparisonRow]:
        """Recompute every investment comparison from today's working paper.

        Read-only: recording or replacing TaxAct entries is the reviewer's
        action (``TaxWorkspaceService.record_reconciliation_item``), retained
        there with its own history.
        """
        paper = self.working_paper()
        recorded = {
            (i.tax_section, i.field_key): i
            for i in self.session.exec(
                select(ReconciliationItem)
                .where(ReconciliationItem.workspace_id == self.workspace_id)
                .execution_options(populate_existing=True)
            ).all()
            if i.status != ReconciliationStatus.STALE
            and i.tax_section in INVESTMENT_TAX_SECTIONS
        }

        rows: list[ComparisonRow] = []
        # Precise members per key: a total's evidence is only the lots that
        # contribute to it, so an empty term is NOT_REQUIRED rather than a
        # mandatory "0 = 0" comparison.
        short_ids = tuple(s.sale_id for s in paper.sales if s.term == "SHORT_TERM")
        long_ids = tuple(s.sale_id for s in paper.sales if s.term == "LONG_TERM")

        def known_ids(term: str) -> tuple[str, ...]:
            return tuple(
                s.sale_id for s in paper.sales if s.term == term and not s.basis_unknown
            )

        members_by_key: dict[str, tuple[str, ...]] = {
            "proceeds_short_term": short_ids,
            "proceeds_long_term": long_ids,
            "basis_reported_short_term": known_ids("SHORT_TERM"),
            "basis_reported_long_term": known_ids("LONG_TERM"),
            "wash_sale_disallowed": tuple(
                s.sale_id for s in paper.sales if s.wash_sale_disallowed_cents
            ),
            "gain_reported_short_term": known_ids("SHORT_TERM"),
            "gain_reported_long_term": known_ids("LONG_TERM"),
            "net_gain_short_term": known_ids("SHORT_TERM"),
            "net_gain_long_term": known_ids("LONG_TERM"),
        }
        for section, key in self.expected_comparison_keys():
            expected = paper.totals.get(key, 0)
            member_ids = tuple(
                line.field_id for line in paper.income_lines if line.key == key
            ) or members_by_key.get(key, ())
            item = recorded.pop((section, key), None)
            if item is None:
                rows.append(
                    ComparisonRow(
                        tax_section=section,
                        field_key=key,
                        working_paper_cents=expected,
                        taxact_entered_cents=None,
                        delta_cents=None,
                        status=NOT_RECORDED,
                        note="no reviewed TaxAct entry recorded for this figure yet",
                        member_ids=member_ids,
                    )
                )
                continue
            if item.source_working_paper_cents != expected:
                rows.append(
                    ComparisonRow(
                        tax_section=section,
                        field_key=key,
                        working_paper_cents=expected,
                        taxact_entered_cents=item.taxact_entered_cents,
                        delta_cents=None,
                        status=SOURCE_DRIFT,
                        note=(
                            "the working paper changed since this entry was recorded "
                            f"({item.source_working_paper_cents} then, {expected} now); "
                            "re-review and re-record the comparison"
                        ),
                        member_ids=member_ids,
                    )
                )
                continue
            delta = item.taxact_entered_cents - expected
            synthesized = (
                "TaxAct matches the reviewed working paper"
                if delta == 0
                else f"TaxAct differs by ${abs(delta) / 100:,.2f} "
                f"({'over' if delta > 0 else 'under'}-entered)"
            )
            rows.append(
                ComparisonRow(
                    tax_section=section,
                    field_key=key,
                    working_paper_cents=expected,
                    taxact_entered_cents=item.taxact_entered_cents,
                    delta_cents=delta,
                    status=MATCHED if delta == 0 else DISCREPANCY,
                    # The reviewer's recorded explanation travels with the
                    # entry; the synthesized note is only the fallback.
                    note=str(item.explanation or "").strip() or synthesized,
                    member_ids=member_ids,
                )
            )

        # Recorded investment comparisons this paper cannot stand behind:
        # a typo, a renamed key, or a figure whose evidence is gone.
        for (section, key), item in sorted(recorded.items()):
            rows.append(
                ComparisonRow(
                    tax_section=section,
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


def _field_citation(f: ExtractedField, doc: TaxDocument | None) -> str:
    name = doc.file_name if doc else "unknown document"
    return f"{name} p.{f.page_number} {f.box_or_line or f.field_name}".strip()


def _sale_citation(s: InvestmentSaleRecord, doc: TaxDocument | None) -> str:
    name = doc.file_name if doc else "unknown document"
    cusip = f" CUSIP {s.cusip_or_ticker}" if s.cusip_or_ticker else ""
    return f"{name}{cusip} sold {s.date_sold}".strip()


def _first_citation(income_by_key: dict[str, list[IncomeLine]], key: str) -> str:
    lines = income_by_key.get(key)
    return lines[0].citation if lines else "no source line recorded"
