"""Annual reconciliation: exact, reproducible, traceable tax-year totals.

Three ideas do the work here.

**Source-form totals and bank-flow totals are different numbers and never
added together.** What a 1099 says and what the bank moved are separate
evidences; a brokerage transfer is a real cash movement and no income at all.
:meth:`ReconciliationService.compute_run` therefore produces one run per basis,
and a total is only ever compared against a total of the same basis.

**Every total carries its members.** ``ReconciliationTotal.member_ids_json``
names the field, transaction or classification rows that produced the number, so
any figure can be taken apart to the evidence underneath it. A total nobody can
trace is not reviewable.

**Unknown is not zero.** Amounts that cannot be decided are counted and
reported separately (:attr:`ReconciliationRun.unresolved_cents`) instead of
being folded into a total that then looks complete.

All arithmetic is integer cents in plain Python. No model is imported, and
:func:`assert_no_model_dependency` keeps it that way.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Field, Session, SQLModel, select

from local_ledger.models import Transaction
from local_ledger.tax.classification import (
    ClassificationSubject,
    TaxClassificationService,
)
from local_ledger.tax.models import (
    EvidenceStatus,
    ExtractedField,
    InvestmentSaleRecord,
    RentalExpenseRecord,
    TaxProperty,
    TaxWorkspace,
)

ID_PREFIX = "run-"


class TotalBasis(StrEnum):
    """Which body of evidence a total is computed from."""

    #: Amounts printed on tax forms, from verified extracted fields.
    SOURCE_FORM = "SOURCE_FORM"
    #: Cash movements in the ledger, from reviewed transaction classifications.
    BANK_FLOW = "BANK_FLOW"


class RunState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Total:
    """A computed figure plus the ids that produced it."""

    field_key: str
    amount_cents: int
    member_ids: tuple[str, ...] = ()
    excluded_ids: tuple[str, ...] = ()
    note: str = ""


@dataclass
class RunReport:
    """What a computation produced, before it is persisted."""

    basis: str
    totals: list[Total] = field(default_factory=list)
    unresolved_cents: int = 0
    unresolved_count: int = 0
    member_count: int = 0
    content_hash: str = ""


class ReconciliationRun(SQLModel, table=True):
    """A computed snapshot of one basis for one workspace.

    ``content_hash`` is computed over the totals and their members, so two runs
    over unchanged evidence hash identically and a changed run is detectable
    without diffing every number.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"{ID_PREFIX}{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(index=True)
    tax_year: int = Field(index=True)
    basis: str = Field(index=True)
    computed_at: datetime = Field(default_factory=_utcnow)
    content_hash: str = Field(index=True)
    state: str = Field(default=RunState.CURRENT, index=True)
    unresolved_cents: int = 0
    unresolved_count: int = 0
    totals_json: str = "[]"


class ReconciliationError(Exception):
    """A reconciliation was refused: missing workspace, bad basis, bad input."""


def assert_no_model_dependency(module_name: str = __name__) -> None:
    """Fail loudly if this module ever grows a model dependency.

    Totals are arithmetic on reviewed evidence. A model in this path would make
    a tax figure non-reproducible, which is the failure this module exists to
    make impossible.
    """
    import sys

    module = sys.modules.get(module_name)
    if module is None:  # pragma: no cover - defensive
        return
    for value in vars(module).values():
        name = getattr(value, "__module__", "") or ""
        if name.startswith("local_ledger.llm"):
            raise ReconciliationError(
                f"{module_name} must not depend on {name}: tax arithmetic stays deterministic"
            )


class ReconciliationService:
    """Computes and stores annual tax totals for one workspace."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.workspace: TaxWorkspace | None = session.get(TaxWorkspace, workspace_id)
        if self.workspace is None:
            raise ReconciliationError(f"workspace {workspace_id} not found")
        self.tax_year = self.workspace.tax_year
        self.classifications = TaxClassificationService(session, workspace_id)

    # ------------------------------------------------------------------
    # source-form totals: what the documents say
    # ------------------------------------------------------------------

    def source_form_totals(self, field_name: str | None = None) -> RunReport:
        """Totals from verified extracted fields, grouped by field name.

        Only ``VERIFIED`` fields count. PROPOSED, REJECTED and SUPERSEDED fields
        are counted in the unresolved figure instead, so a report cannot look
        complete while a correction is outstanding.
        """
        stmt = select(ExtractedField).where(ExtractedField.workspace_id == self.workspace_id)
        if field_name:
            stmt = stmt.where(ExtractedField.field_name == field_name)
        fields = list(self.session.exec(stmt).all())

        grouped: dict[str, list[ExtractedField]] = {}
        for f in fields:
            grouped.setdefault(f.field_name, []).append(f)

        totals: list[Total] = []
        unresolved_cents = 0
        unresolved_count = 0
        for name, rows in sorted(grouped.items()):
            verified = [r for r in rows if r.status == EvidenceStatus.VERIFIED]
            excluded = [r for r in rows if r.status != EvidenceStatus.VERIFIED]
            excluded_cents = sum(r.amount_cents for r in excluded)
            unresolved_cents += excluded_cents
            unresolved_count += len(excluded)
            totals.append(
                Total(
                    field_key=name,
                    amount_cents=sum(r.amount_cents for r in verified),
                    member_ids=tuple(r.id for r in verified),
                    excluded_ids=tuple(r.id for r in excluded),
                    note=(
                        f"{len(excluded)} of {len(rows)} source field(s) not verified; "
                        "excluded from this total"
                        if excluded
                        else ""
                    ),
                )
            )
        return RunReport(basis=TotalBasis.SOURCE_FORM, totals=totals, unresolved_cents=unresolved_cents, unresolved_count=unresolved_count)

    # ------------------------------------------------------------------
    # bank-flow totals: what the ledger moved
    # ------------------------------------------------------------------

    def _year_transactions(self, account_ids: list[int]) -> list[Transaction]:
        if not account_ids:
            return []
        stmt = select(Transaction).where(Transaction.account_id.in_(account_ids))
        rows = list(self.session.exec(stmt).all())
        prefix = f"{self.tax_year}-"
        return [r for r in rows if r.date.startswith(prefix)]

    def bank_flow_totals(self, account_ids: list[int] | None = None) -> RunReport:
        """Totals from reviewed ledger classifications, excluding transfers.

        Income and excluded flows are reported under separate field keys so a
        caller cannot add a transfer into an income total by accident:
        ``income:<treatment>`` and ``excluded:<treatment>``.
        """
        if account_ids is None:
            account_ids = self._accounts_referenced()
        transactions = {t.id: t for t in self._year_transactions(account_ids)}
        classifications = self.classifications.all_records()

        income: dict[str, Total] = {}
        excluded: dict[str, Total] = {}
        unresolved_cents = 0
        unresolved_count = 0

        for record in classifications:
            if record.subject_type != ClassificationSubject.TRANSACTION:
                continue
            if record.state != "CLASSIFIED" or record.treatment is None:
                unresolved_cents += abs(record.amount_cents)
                unresolved_count += 1
                continue
            tx = transactions.get(_int_id(record.subject_id))
            if tx is None:
                # Classified against a row outside this tax year, or deleted.
                unresolved_cents += abs(record.amount_cents)
                unresolved_count += 1
                continue
            amount = self.classifications.allocation_cents(record)
            bucket = (
                excluded
                if record.treatment in _excluded_treatments()
                else income
            )
            prefix = "excluded" if bucket is excluded else "income"
            key = f"{prefix}:{record.treatment}"
            slot = bucket.get(key) or Total(
                field_key=key, amount_cents=0, member_ids=(), excluded_ids=()
            )
            bucket[key] = Total(
                field_key=key,
                amount_cents=slot.amount_cents + amount,
                member_ids=(*slot.member_ids, record.id),
                excluded_ids=slot.excluded_ids,
                note=slot.note,
            )

        totals = sorted(income.values(), key=lambda t: t.field_key)
        totals += sorted(excluded.values(), key=lambda t: t.field_key)
        return RunReport(
            basis=TotalBasis.BANK_FLOW,
            totals=totals,
            unresolved_cents=unresolved_cents,
            unresolved_count=unresolved_count,
        )

    def _accounts_referenced(self) -> list[int]:
        """Accounts that this workspace's classifications actually touch."""
        from local_ledger.tax.models import ReconciliationItem

        ids: set[int] = set()
        stmt = select(ReconciliationItem).where(
            ReconciliationItem.workspace_id == self.workspace_id,
            ReconciliationItem.consumed_transaction_ids_json.is_not(None),
        )
        for item in self.session.exec(stmt).all():
            for raw in json.loads(item.consumed_transaction_ids_json or "[]"):
                tx = self.session.get(Transaction, int(raw))
                if tx is not None:
                    ids.add(tx.account_id)
        return sorted(ids)

    # ------------------------------------------------------------------
    # rental working papers
    # ------------------------------------------------------------------

    def rental_totals(self, property_id: str | None = None) -> RunReport:
        """Per-property income, deductible expenses, and visibly-not-expenses.

        Deducibility is not decided here. The buckets are ``deductible:<code>``
        for treatments a review classified as expenses, ``not_deductible:<code>``
        for principal and capital improvements, and ``unresolved`` for the rest.
        A capital-improvement suspect is therefore never allowed to sit quietly
        in an expense total.
        """
        properties = {
            p.id: p
            for p in self.session.exec(
                select(TaxProperty).where(TaxProperty.workspace_id == self.workspace_id)
            ).all()
        }
        if property_id is not None:
            if property_id not in properties:
                raise ReconciliationError(f"property {property_id} is not in this workspace")
            properties = {property_id: properties[property_id]}

        deductible: dict[str, Total] = {}
        not_deductible: dict[str, Total] = {}
        unresolved: dict[str, Total] = {}
        unresolved_cents = 0
        unresolved_count = 0

        expenses = self.session.exec(
            select(RentalExpenseRecord).where(
                RentalExpenseRecord.workspace_id == self.workspace_id
            )
        ).all()
        by_expense = {e.id: e for e in expenses}

        unresolved_rows = self.classifications.unresolved()
        for record in unresolved_rows:
            if record.subject_type != ClassificationSubject.RENTAL_EXPENSE:
                continue
            if property_id is not None and record.property_id != property_id:
                continue
            amount = self.classifications.allocation_cents(record)
            key = f"unresolved:{record.property_id or 'workspace'}"
            slot = unresolved.get(key)
            unresolved[key] = Total(
                field_key=key,
                amount_cents=(slot.amount_cents if slot else 0) + amount,
                member_ids=((slot.member_ids if slot else ()) + (record.id,)),
            )
            unresolved_cents += amount
            unresolved_count += 1

        for record in self.classifications.classified():
            if record.subject_type != ClassificationSubject.RENTAL_EXPENSE:
                continue
            if property_id is not None and record.property_id != property_id:
                continue
            amount = self.classifications.allocation_cents(record)
            code = record.treatment
            if code in _not_deductible_treatments():
                key = f"not_deductible:{code}:{record.property_id or 'workspace'}"
                bucket = not_deductible
            else:
                key = f"deductible:{code}:{record.property_id or 'workspace'}"
                bucket = deductible
            slot = bucket.get(key)
            bucket[key] = Total(
                field_key=key,
                amount_cents=(slot.amount_cents if slot else 0) + amount,
                member_ids=((slot.member_ids if slot else ()) + (record.id,)),
                note=f"expense {record.subject_id}" if record.subject_id in by_expense else "",
            )

        totals = (
            sorted(deductible.values(), key=lambda t: t.field_key)
            + sorted(not_deductible.values(), key=lambda t: t.field_key)
            + sorted(unresolved.values(), key=lambda t: t.field_key)
        )
        return RunReport(
            basis=TotalBasis.SOURCE_FORM,
            totals=totals,
            unresolved_cents=unresolved_cents,
            unresolved_count=unresolved_count,
        )

    # ------------------------------------------------------------------
    # investment totals
    # ------------------------------------------------------------------

    def investment_totals(self) -> RunReport:
        """Proceeds, reported basis and undetermined basis, kept apart.

        A noncovered lot with no reported basis is ``None`` in the persistence
        layer; here it is counted in ``basis_not_reported_lots`` and its
        proceeds still appear. Summing a null basis as zero would produce a
        total that reads as complete and is not.
        """
        sales = self.session.exec(
            select(InvestmentSaleRecord).where(
                InvestmentSaleRecord.workspace_id == self.workspace_id,
                InvestmentSaleRecord.status == EvidenceStatus.VERIFIED,
            )
        ).all()
        if not sales:
            return RunReport(basis=TotalBasis.SOURCE_FORM, totals=[])

        short = [s for s in sales if s.term == "SHORT_TERM"]
        long_ = [s for s in sales if s.term == "LONG_TERM"]
        totals = [
            _sale_total("proceeds_short_term", short),
            _sale_total("proceeds_long_term", long_),
            _sale_total("basis_reported_short_term", [s for s in short if s.cost_basis_cents is not None]),
            _sale_total("basis_reported_long_term", [s for s in long_ if s.cost_basis_cents is not None]),
        ]
        unknown = [s for s in sales if s.cost_basis_cents is None]
        wash = sum(s.wash_sale_disallowed_cents for s in sales)
        totals.append(
            Total(
                field_key="wash_sale_disallowed",
                amount_cents=wash,
                member_ids=tuple(s.id for s in sales if s.wash_sale_disallowed_cents),
                note="reported on Form 8949; requires review of the replacement purchase",
            )
        )
        return RunReport(
            basis=TotalBasis.SOURCE_FORM,
            totals=totals,
            unresolved_cents=sum(s.proceeds_cents for s in unknown),
            unresolved_count=len(unknown),
        )

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def compute_run(self, basis: str) -> RunReport:
        """Compute (without persisting) the totals for one basis."""
        assert_no_model_dependency()
        if basis == TotalBasis.SOURCE_FORM:
            report = self.source_form_totals()
        elif basis == TotalBasis.BANK_FLOW:
            report = self.bank_flow_totals()
        else:
            raise ReconciliationError(f"unknown basis {basis!r}")
        report.member_count = sum(len(t.member_ids) for t in report.totals)
        report.content_hash = self._hash(report)
        return report

    def persist(self, report: RunReport) -> ReconciliationRun:
        run = ReconciliationRun(
            workspace_id=self.workspace_id,
            tax_year=self.tax_year,
            basis=report.basis,
            content_hash=report.content_hash,
            state=RunState.CURRENT,
            unresolved_cents=report.unresolved_cents,
            unresolved_count=report.unresolved_count,
            totals_json=json.dumps(
                [
                    {
                        "field_key": t.field_key,
                        "amount_cents": t.amount_cents,
                        "member_ids": list(t.member_ids),
                        "excluded_ids": list(t.excluded_ids),
                        "note": t.note,
                    }
                    for t in report.totals
                ],
                sort_keys=True,
            ),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def current_runs(self, basis: str | None = None) -> list[ReconciliationRun]:
        stmt = select(ReconciliationRun).where(
            ReconciliationRun.workspace_id == self.workspace_id,
            ReconciliationRun.state == RunState.CURRENT,
        )
        if basis is not None:
            stmt = stmt.where(ReconciliationRun.basis == basis)
        return list(self.session.exec(stmt).all())

    def totals_of(self, run: ReconciliationRun) -> list[Total]:
        return [
            Total(
                field_key=row["field_key"],
                amount_cents=row["amount_cents"],
                member_ids=tuple(row.get("member_ids", ())),
                excluded_ids=tuple(row.get("excluded_ids", ())),
                note=row.get("note", ""),
            )
            for row in json.loads(run.totals_json)
        ]

    def mark_stale(self, run_ids: list[str] | None = None) -> int:
        """Mark runs stale after the evidence beneath them changed."""
        stmt = select(ReconciliationRun).where(
            ReconciliationRun.workspace_id == self.workspace_id,
            ReconciliationRun.state == RunState.CURRENT,
        )
        if run_ids is not None:
            stmt = stmt.where(ReconciliationRun.id.in_(run_ids))
        count = 0
        for run in self.session.exec(stmt).all():
            run.state = RunState.STALE
            self.session.add(run)
            count += 1
        if count:
            self.session.commit()
        return count

    def _hash(self, report: RunReport) -> str:
        payload = json.dumps(
            {
                "workspace_id": self.workspace_id,
                "tax_year": self.tax_year,
                "basis": report.basis,
                "unresolved_cents": report.unresolved_cents,
                "unresolved_count": report.unresolved_count,
                "totals": sorted(
                    (
                        {
                            "field_key": t.field_key,
                            "amount_cents": t.amount_cents,
                            "member_ids": sorted(t.member_ids),
                        }
                        for t in report.totals
                    ),
                    key=lambda r: r["field_key"],
                ),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sale_total(key: str, sales: list[InvestmentSaleRecord]) -> Total:
    amount = 0
    for s in sales:
        if key.startswith("proceeds"):
            amount += s.proceeds_cents
        else:
            amount += s.cost_basis_cents or 0
    return Total(
        field_key=key,
        amount_cents=amount,
        member_ids=tuple(s.id for s in sales),
        note="basis not reported is excluded from this total, not treated as zero"
        if key.startswith("basis")
        else "",
    )


def _int_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _excluded_treatments() -> frozenset[str]:
    from local_ledger.tax.classification import INCOME_EXCLUDED_TREATMENTS

    return frozenset(str(t) for t in INCOME_EXCLUDED_TREATMENTS)


def _not_deductible_treatments() -> frozenset[str]:
    from local_ledger.tax.classification import TaxTreatment

    return frozenset(
        {
            str(TaxTreatment.MORTGAGE_PRINCIPAL),
            str(TaxTreatment.CAPITAL_IMPROVEMENT),
            str(TaxTreatment.DEPRECIATION),
        }
    )
