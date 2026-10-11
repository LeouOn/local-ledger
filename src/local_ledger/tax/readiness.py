"""Deterministic review readiness shared by packet creation and callers."""

from dataclasses import dataclass

from sqlmodel import Session, select

from local_ledger.tax.classification import ClassificationSubject, DecisionHead
from local_ledger.tax.models import (
    DocumentType,
    EvidenceStatus,
    ExtractedField,
    InvestmentSaleRecord,
    ReconciliationItem,
    ReconciliationStatus,
    RentalExpenseRecord,
    TaxDocument,
    TaxQuestion,
)
from local_ledger.tax.reconciliation import ReconciliationService


@dataclass(frozen=True)
class ReviewReadiness:
    reasons: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons

    def to_snapshot(self) -> dict:
        return {"ready": self.ready, "reasons": list(self.reasons)}


def evaluate_readiness(session: Session, workspace_id: str) -> ReviewReadiness:
    """Recompute obligations from current sources; saved CURRENT flags are insufficient.

    This evaluates review completeness, not legal correctness or filing readiness.
    Comparison coverage per TaxAct field remains the TAX-08 mapping boundary.
    """
    rec = ReconciliationService(session, workspace_id)
    scope = rec.scope()
    reasons: list[str] = []
    if not scope.is_configured:
        reasons.append("review scope is UNCONFIGURED")
    if not scope.properties_confirmed:
        reasons.append("rental property scope has not been confirmed")

    questions = session.exec(
        select(TaxQuestion).where(
            TaxQuestion.workspace_id == workspace_id,
            TaxQuestion.status == "OPEN",
        ),
        execution_options={"populate_existing": True},
    ).all()
    if questions:
        reasons.append(f"{len(questions)} open question(s) remain")

    fields = session.exec(
        select(ExtractedField).where(
            ExtractedField.workspace_id == workspace_id,
        ),
        execution_options={"populate_existing": True},
    ).all()
    sales = session.exec(
        select(InvestmentSaleRecord).where(
            InvestmentSaleRecord.workspace_id == workspace_id,
        ),
        execution_options={"populate_existing": True},
    ).all()
    expenses = session.exec(
        select(RentalExpenseRecord).where(
            RentalExpenseRecord.workspace_id == workspace_id,
        ),
        execution_options={"populate_existing": True},
    ).all()
    selected_expenses = [r for r in expenses if scope.covers_property(r.property_id)]
    pending = sum(
        r.status == EvidenceStatus.PROPOSED for r in [*fields, *sales, *selected_expenses]
    )
    if pending:
        reasons.append(f"{pending} pending source evidence record(s)")

    for name, report in (
        ("bank", rec.bank_flow_totals()),
        ("rental", rec.rental_totals()),
        ("investment basis", rec.investment_totals()),
    ):
        if report.unresolved_count:
            reasons.append(f"{report.unresolved_count} unresolved {name} subject(s)")

    # Previously reviewed investment measures must still match their sources.
    # Bank/rental population checks above also find subjects without any head.
    heads = session.exec(
        select(DecisionHead).where(DecisionHead.workspace_id == workspace_id),
        execution_options={"populate_existing": True},
    ).all()
    for head in sorted(heads, key=lambda h: h.id):
        if head.subject_type == ClassificationSubject.INVESTMENT_SALE:
            sale = next((r for r in sales if r.id == head.subject_id), None)
            if (
                sale
                and sale.status not in (EvidenceStatus.REJECTED, EvidenceStatus.SUPERSEDED)
                and not rec._decisions_for_subject(head.subject_type, head.subject_id, head.measure)
            ):
                reasons.append(
                    f"investment decision {head.subject_id}:{head.measure} needs re-review"
                )

    # A corrected document without replacement rows is a gap even though its
    # superseded history is correctly excluded from pending counts.
    docs = session.exec(select(TaxDocument).where(TaxDocument.workspace_id == workspace_id)).all()
    superseded_docs = {d.supersedes_doc_id for d in docs if d.supersedes_doc_id}
    active_source_ids = {
        r.document_id
        for r in fields
        if r.status
        in (
            EvidenceStatus.PROPOSED,
            EvidenceStatus.VERIFIED,
        )
    }
    active_source_ids.update(
        r.source_document_id
        for r in [*sales, *expenses]
        if r.status
        in (
            EvidenceStatus.PROPOSED,
            EvidenceStatus.VERIFIED,
        )
    )
    for doc in sorted(docs, key=lambda d: d.id):
        if doc.id in superseded_docs or doc.id in active_source_ids:
            continue
        if doc.is_corrected:
            reasons.append(f"corrected document {doc.id} has no replacement evidence")
        elif doc.doc_type in (
            DocumentType.FORM_1099_CONSOLIDATED,
            DocumentType.FORM_1099_B,
            DocumentType.FORM_1099_DIV,
            DocumentType.FORM_1099_INT,
            DocumentType.FORM_1098_MORTGAGE,
            DocumentType.RENTAL_ANNUAL_STATEMENT,
            DocumentType.PROPERTY_TAX_BILL,
        ):
            reasons.append(f"source document {doc.id} has no reviewed evidence")

    comparisons = session.exec(
        select(ReconciliationItem).where(
            ReconciliationItem.workspace_id == workspace_id,
        ),
        execution_options={"populate_existing": True},
    ).all()
    if not comparisons:
        reasons.append("no current TaxAct comparisons recorded")
    grouped: dict[tuple[str, str], list[ReconciliationItem]] = {}
    for item in comparisons:
        grouped.setdefault((item.tax_section, item.field_key), []).append(item)
    for (_, field_key), history in sorted(grouped.items()):
        current = [r for r in history if r.status != ReconciliationStatus.STALE]
        if len(current) != 1:
            reasons.append(f"TaxAct comparison {field_key} needs a current review")
            continue
        item = current[0]
        if (
            item.status != ReconciliationStatus.MATCHED
            or item.delta_cents != 0
            or item.source_working_paper_cents != item.taxact_entered_cents
        ):
            reasons.append(f"TaxAct comparison {field_key} is not currently matched")
    return ReviewReadiness(tuple(reasons))
