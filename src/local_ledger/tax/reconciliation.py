"""Annual reconciliation: exact, reproducible, traceable tax-year totals.

Five rules do the work here, and each exists because its absence produced a
silently wrong number.

**A total is computed from a scope, not from what happens to exist.** The old
code iterated classification records, so a transaction nobody had classified was
simply absent - not counted, not reported. Now the workspace persists its review
boundary, and every tax-year row inside it is accounted for exactly once:
reviewed, excluded, or **unresolved**. An unconfigured scope cannot produce a
complete run at all, because "we looked at nothing" and "we looked and found
nothing" must not look the same.

**Only the current revision of a decision participates.** History is retained
and displayed, but summing it would double count every superseded decision. So
readers resolve each decision through its head.

**Amounts are read from the source, not copied.** A decision records the
fingerprint of the source it reviewed. If the source has changed since, the
decision is not counted - approval is never carried forward onto a new amount.

**Investment measures stay distinct.** Proceeds cannot supply cost basis, and a
lot with no reported basis stays unknown rather than being filled in.

**All arithmetic is integer cents in plain code**, and
:func:`assert_no_model_dependency` keeps a model out of this path.
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

from local_ledger.tax.classification import (
    ClassificationError,
    ClassificationState,
    ClassificationSubject,
    Measure,
    TaxClassification,
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
from local_ledger.tax.scope import ReviewScope, ReviewScopeMode, WorkspaceScopeService

ID_PREFIX = "run-"


class TotalBasis(StrEnum):
    """Which body of evidence a total is computed from."""

    SOURCE_FORM = "SOURCE_FORM"
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
    scope_version: int | None = None
    scope_snapshot: dict | None = None
    complete: bool = True
    incompleteness_reason: str | None = None


class ReconciliationRun(SQLModel, table=True):
    """A computed snapshot of one basis for one workspace.

    ``content_hash`` covers the totals *and their members*, so a figure can be
    taken apart to the evidence beneath it and a changed run is detectable
    without diffing every number. The scope version and snapshot are stored too,
    so a run can still explain what it covered after the scope moves.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"{ID_PREFIX}{uuid.uuid4().hex[:12]}", primary_key=True)
    workspace_id: str = Field(index=True)
    tax_year: int = Field(index=True)
    basis: str = Field(index=True)
    computed_at: datetime = Field(default_factory=_utcnow)
    content_hash: str = Field(index=True)
    state: str = Field(default=RunState.CURRENT, index=True)
    scope_version: int | None = Field(default=None)
    scope_snapshot_json: str | None = Field(default=None)
    complete: bool = True
    incompleteness_reason: str | None = Field(default=None)
    unresolved_cents: int = 0
    unresolved_count: int = 0
    totals_json: str = "[]"


class ReconciliationError(Exception):
    """A reconciliation was refused."""


def assert_no_model_dependency(module_name: str = __name__) -> None:
    """Fail loudly if this module ever grows a model dependency.

    Tax totals are arithmetic on reviewed evidence. A model in this path would
    make a figure non-reproducible, which is the failure this module exists to
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
        self.scopes = WorkspaceScopeService(session)

    # ------------------------------------------------------------------
    # scope
    # ------------------------------------------------------------------

    def scope(self) -> ReviewScope:
        return self.scopes.get(self.workspace_id)

    def _incomplete(self, reason: str, basis: str = TotalBasis.BANK_FLOW) -> RunReport:
        """A report that refuses to claim completeness, with no totals."""
        return RunReport(
            basis=basis,
            totals=[],
            scope_version=self.scope().version,
            scope_snapshot=self.scope().to_snapshot(),
            unresolved_count=0,
            complete=False,
            incompleteness_reason=reason,
        )

    def _scope_transactions(self, scope: ReviewScope) -> list:  # type: ignore[no-untyped-def]
        """Every transaction of the selected accounts in this tax year.

        The whole in-scope population, not just the classified part - that is
        what makes an unclassified row visible instead of absent.
        """
        from local_ledger.models import Transaction

        # `==`, never `is`: a StrEnum member loaded from the database is a plain
        # string, so identity comparison silently fails on stored values.
        if scope.mode != ReviewScopeMode.SELECTED_ACCOUNTS:
            return []
        if not scope.account_ids:
            return []
        rows = self.session.exec(
            select(Transaction).where(Transaction.account_id.in_(scope.account_ids)).execution_options(populate_existing=True)
        ).all()
        prefix = f"{self.tax_year}-"
        return [r for r in rows if r.date.startswith(prefix)]

    # ------------------------------------------------------------------
    # decision reading: current revision only, source still current
    # ------------------------------------------------------------------

    def _decisions_for_subject(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> list[TaxClassification]:
        """The current decision for a subject, or [] if it is not countable.

        Empty covers three cases that must all stay out of totals: no decision,
        a decision whose source no longer matches what was reviewed, and a
        decision explicitly parked as needing review.
        """
        parts = self.classifications.current_revision_parts(subject_type, subject_id, measure)
        if not parts:
            return []
        if any(p.state != ClassificationState.CLASSIFIED for p in parts):
            return []
        if any(p.source_fingerprint is None for p in parts):
            return []
        try:
            resolved = self.classifications.resolve_subject(subject_type, subject_id, measure)
        except ClassificationError:
            return []
        # Approval never carries forward onto a changed amount.
        if (any(p.source_fingerprint != resolved.fingerprint for p in parts)
                or sum(p.allocation_bp for p in parts) != 10000):
            return []
        return parts

    def _allocation_map(
        self, subject_type: str, subject_id: str, measure: str = Measure.AMOUNT
    ) -> dict[str, int]:
        return self.classifications.allocations_for(subject_type, subject_id, measure)

    # ------------------------------------------------------------------
    # bank flow: scope-driven completeness
    # ------------------------------------------------------------------

    def bank_flow_totals(self) -> RunReport:
        """Income, excluded flows and unresolved, over the configured scope.

        Income and excluded flows use different field-key prefixes so a caller
        cannot add a transfer into an income total by accident.
        """
        scope = self.scope()
        if not scope.is_configured:
            return self._incomplete(
                "review scope is UNCONFIGURED; totals cannot be claimed complete until the "
                "accounts and properties in scope are set"
            )
        if (
            scope.mode != ReviewScopeMode.DOCUMENTS_ONLY
            and not scope.account_ids
            and not scope.properties_confirmed
        ):
            return self._incomplete(
                "scope is SELECTED_ACCOUNTS with no accounts; an empty account list is "
                "not the same as 'we looked and found nothing'"
            )

        transactions = self._scope_transactions(scope)
        totals: dict[str, Total] = {}
        unresolved_cents = 0
        unresolved_subjects: set[str] = set()

        for tx in transactions:
            subject_id = str(tx.id)
            parts = self._decisions_for_subject(ClassificationSubject.TRANSACTION, subject_id)
            if not parts:
                # Not decided, or decided against a source that has since changed.
                # Either way it is visible, not absent.
                unresolved_cents += abs(tx.amount_cents)
                unresolved_subjects.add(f"TRANSACTION:{subject_id}")
                continue
            allocations = self._allocation_map(ClassificationSubject.TRANSACTION, subject_id)
            for part in parts:
                amount = allocations.get(part.id, 0)
                excluded = part.treatment is not None and str(part.treatment) in _excluded()
                prefix = "excluded" if excluded else "income"
                key = f"{prefix}:{part.treatment}"
                slot = totals.get(key) or Total(field_key=key, amount_cents=0, member_ids=())
                totals[key] = Total(
                    field_key=key,
                    amount_cents=slot.amount_cents + amount,
                    member_ids=(*slot.member_ids, part.id),
                    note=slot.note,
                )

        ordered = [totals[k] for k in sorted(totals)]
        return RunReport(
            basis=TotalBasis.BANK_FLOW,
            totals=ordered,
            unresolved_cents=unresolved_cents,
            # Distinct active subjects, not split parts and not history.
            unresolved_count=len(unresolved_subjects),
            scope_version=scope.version,
            scope_snapshot=scope.to_snapshot(),
            complete=scope.is_configured,
            incompleteness_reason=None if scope.is_configured else "review scope is UNCONFIGURED",
        )

    # ------------------------------------------------------------------
    # source form
    # ------------------------------------------------------------------

    def source_form_totals(self, field_name: str | None = None) -> RunReport:
        """Totals from verified extracted fields, grouped by field name.

        Only VERIFIED fields count; PROPOSED fields remain pending. Rejected
        and superseded history is excluded without creating new obligations.
        Readiness separately checks missing replacement evidence.
        """
        stmt = select(ExtractedField).where(ExtractedField.workspace_id == self.workspace_id)
        if field_name:
            stmt = stmt.where(ExtractedField.field_name == field_name)
        rows = list(self.session.exec(stmt.execution_options(populate_existing=True)).all())

        grouped: dict[str, list[ExtractedField]] = {}
        for row in rows:
            grouped.setdefault(row.field_name, []).append(row)

        totals: list[Total] = []
        unresolved_cents = 0
        unresolved_count = 0
        for name, group in sorted(grouped.items()):
            verified = [r for r in group if r.status == EvidenceStatus.VERIFIED]
            pending = [r for r in group if r.status == EvidenceStatus.PROPOSED]
            excluded = [r for r in group if r.status != EvidenceStatus.VERIFIED]
            unresolved_cents += sum(r.amount_cents for r in pending)
            unresolved_count += len(pending)
            totals.append(
                Total(
                    field_key=name,
                    amount_cents=sum(r.amount_cents for r in verified),
                    member_ids=tuple(r.id for r in verified),
                    excluded_ids=tuple(r.id for r in excluded),
                    note=(
                        f"{len(pending)} of {len(group)} source field(s) not verified; "
                        "excluded from this total"
                        if pending
                        else ""
                    ),
                )
            )
        scope = self.scope()
        return RunReport(
            basis=TotalBasis.SOURCE_FORM,
            totals=totals,
            unresolved_cents=unresolved_cents,
            unresolved_count=unresolved_count,
            scope_version=scope.version,
            scope_snapshot=scope.to_snapshot(),
            complete=scope.is_configured,
            incompleteness_reason=None if scope.is_configured else "review scope is UNCONFIGURED",
        )

    # ------------------------------------------------------------------
    # rental working papers
    # ------------------------------------------------------------------

    def rental_totals(self, property_id: str | None = None) -> RunReport:
        """Per-property income, deductible expenses, and visibly-not-expenses.

        Every ``RentalExpenseRecord`` in scope is accounted for: approved into a
        deductible or not-deductible bucket, or unresolved. Deducibility is never
        decided here - the buckets record what review decided.
        """
        scope = self.scope()
        if property_id is not None:
            prop = self.session.get(TaxProperty, property_id)
            if prop is None or prop.workspace_id != self.workspace_id:
                raise ReconciliationError(f"property {property_id} is not in this workspace")
        if not scope.is_configured:
            return self._incomplete(
                "review scope is UNCONFIGURED; select rental properties before totaling",
                TotalBasis.SOURCE_FORM,
            )
        if property_id is not None and not scope.covers_property(property_id):
            raise ReconciliationError(f"property {property_id} is outside the review scope")
        properties = {
            p.id: p
            for p in self.session.exec(
                select(TaxProperty).where(TaxProperty.workspace_id == self.workspace_id)
            ).all()
        }
        if property_id is not None and property_id not in properties:
            raise ReconciliationError(f"property {property_id} is not in this workspace")

        expenses = list(
            self.session.exec(
                select(RentalExpenseRecord).where(
                    RentalExpenseRecord.workspace_id == self.workspace_id
                ).execution_options(populate_existing=True)
            ).all()
        )
        expenses = [e for e in expenses if scope.covers_property(e.property_id)
                    and e.status not in (EvidenceStatus.SUPERSEDED, EvidenceStatus.REJECTED)]
        if property_id is not None:
            expenses = [e for e in expenses if e.property_id == property_id]

        deductible: dict[str, Total] = {}
        not_deductible: dict[str, Total] = {}
        unresolved: dict[str, Total] = {}
        unresolved_cents = 0
        unresolved_subjects: set[str] = set()

        for expense in expenses:
            subject_id = expense.id
            parts = self._decisions_for_subject(
                ClassificationSubject.RENTAL_EXPENSE, subject_id
            )
            label = expense.property_id or "workspace"
            if not parts:
                amount = expense.amount_cents
                key = f"unresolved:{label}"
                slot = unresolved.get(key) or Total(
                    field_key=key, amount_cents=0, member_ids=()
                )
                unresolved[key] = Total(
                    field_key=key,
                    amount_cents=slot.amount_cents + amount,
                    member_ids=(*slot.member_ids, subject_id),
                    note="no current decided treatment, or its source has changed",
                )
                unresolved_cents += amount
                unresolved_subjects.add(f"RENTAL_EXPENSE:{subject_id}")
                continue

            allocations = self._allocation_map(ClassificationSubject.RENTAL_EXPENSE, subject_id)
            for part in parts:
                amount = allocations.get(part.id, 0)
                code = str(part.treatment)
                if code in _not_deductible():
                    key = f"not_deductible:{code}:{label}"
                    bucket = not_deductible
                else:
                    key = f"deductible:{code}:{label}"
                    bucket = deductible
                slot = bucket.get(key) or Total(field_key=key, amount_cents=0, member_ids=())
                bucket[key] = Total(
                    field_key=key,
                    amount_cents=slot.amount_cents + amount,
                    member_ids=(*slot.member_ids, part.id),
                    note=slot.note,
                )

        totals = (
            sorted(deductible.values(), key=lambda t: t.field_key)
            + sorted(not_deductible.values(), key=lambda t: t.field_key)
            + sorted(unresolved.values(), key=lambda t: t.field_key)
        )
        scope = self.scope()
        return RunReport(
            basis=TotalBasis.SOURCE_FORM,
            totals=totals,
            unresolved_cents=unresolved_cents,
            unresolved_count=len(unresolved_subjects),
            scope_version=scope.version,
            scope_snapshot=scope.to_snapshot(),
            complete=scope.is_configured,
            incompleteness_reason=None if scope.is_configured else "review scope is UNCONFIGURED",
        )

    # ------------------------------------------------------------------
    # investments: measures kept distinct
    # ------------------------------------------------------------------

    def investment_totals(self) -> RunReport:
        """Proceeds, reported basis and undetermined basis, kept apart.

        A noncovered lot with no reported basis contributes its proceeds, and its
        basis is counted as unresolved. Summing a null basis as zero would produce
        a total that reads as complete and is not.
        """
        sales = list(
            self.session.exec(
                select(InvestmentSaleRecord).where(
                    InvestmentSaleRecord.workspace_id == self.workspace_id,
                    InvestmentSaleRecord.status == EvidenceStatus.VERIFIED,
                ).execution_options(populate_existing=True)
            ).all()
        )
        scope = self.scope()
        scope_metadata = dict(
            scope_version=scope.version, scope_snapshot=scope.to_snapshot(),
            complete=scope.is_configured,
            incompleteness_reason=None if scope.is_configured else "review scope is UNCONFIGURED",
        )
        if not sales:
            return RunReport(basis=TotalBasis.SOURCE_FORM, totals=[], **scope_metadata)

        short = [s for s in sales if s.term == "SHORT_TERM"]
        long_ = [s for s in sales if s.term == "LONG_TERM"]
        unknown_basis = [s for s in sales if s.cost_basis_cents is None]
        wash = sum(s.wash_sale_disallowed_cents for s in sales)

        totals = [
            _measure_total("proceeds_short_term", short, Measure.PROCEEDS, self),
            _measure_total("proceeds_long_term", long_, Measure.PROCEEDS, self),
            _measure_total(
                "basis_reported_short_term",
                [s for s in short if s.cost_basis_cents is not None],
                Measure.BASIS,
                self,
            ),
            _measure_total(
                "basis_reported_long_term",
                [s for s in long_ if s.cost_basis_cents is not None],
                Measure.BASIS,
                self,
            ),
            Total(
                field_key="wash_sale_disallowed",
                amount_cents=wash,
                member_ids=tuple(s.id for s in sales if s.wash_sale_disallowed_cents),
                note="reported on Form 8949; requires review of the replacement purchase",
            ),
        ]
        return RunReport(
            basis=TotalBasis.SOURCE_FORM,
            totals=totals,
            # Undetermined basis stays unknown: the proceeds are known, the basis
            # is not, and the difference is not this module's to invent.
            unresolved_cents=sum(s.proceeds_cents for s in unknown_basis),
            unresolved_count=len(unknown_basis),
            **scope_metadata,
        )

    def _allocations_for_sale(self, sale_id: str, measure: str) -> dict[str, int]:
        return self.classifications.allocations_for(
            ClassificationSubject.INVESTMENT_SALE, sale_id, measure
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
            scope_version=report.scope_version,
            scope_snapshot_json=(
                json.dumps(report.scope_snapshot, sort_keys=True) if report.scope_snapshot else None
            ),
            complete=report.complete,
            incompleteness_reason=report.incompleteness_reason,
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
        return list(self.session.exec(stmt.execution_options(populate_existing=True)).all())

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
        stmt = select(ReconciliationRun).where(
            ReconciliationRun.workspace_id == self.workspace_id,
            ReconciliationRun.state == RunState.CURRENT,
        )
        if run_ids is not None:
            stmt = stmt.where(ReconciliationRun.id.in_(run_ids))
        count = 0
        for run in self.session.exec(stmt.execution_options(populate_existing=True)).all():
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
                "scope_version": report.scope_version,
                "complete": report.complete,
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


def _measure_total(
    key: str,
    sales: list[InvestmentSaleRecord],
    measure: str,
    service: ReconciliationService,
) -> Total:
    total = 0
    members: list[str] = []
    for sale in sales:
        parts = service._decisions_for_subject(
            ClassificationSubject.INVESTMENT_SALE, sale.id, measure
        )
        allocations = service._allocations_for_sale(sale.id, measure)
        for part in parts:
            total += allocations.get(part.id, 0)
            members.append(part.id)
    return Total(
        field_key=key,
        amount_cents=total,
        member_ids=tuple(members),
        note=(
            "basis not reported is excluded from this total, not treated as zero"
            if measure == Measure.BASIS
            else ""
        ),
    )


def _excluded() -> frozenset[str]:
    from local_ledger.tax.classification import INCOME_EXCLUDED_TREATMENTS

    return INCOME_EXCLUDED_TREATMENTS


def _not_deductible() -> frozenset[str]:
    from local_ledger.tax.classification import TaxTreatment

    return frozenset(
        {
            str(TaxTreatment.MORTGAGE_PRINCIPAL),
            str(TaxTreatment.CAPITAL_IMPROVEMENT),
            str(TaxTreatment.DEPRECIATION),
        }
    )
