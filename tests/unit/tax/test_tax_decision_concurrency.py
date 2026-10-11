"""TAX-06 decision integrity under concurrency and failure.

The hardening proposal asks for two guarantees that unit tests against one
session cannot establish:

- **Two sessions must not both own the current decision.** The unique head plus
  a compare-and-set version update is the guard; a loser gets a review conflict
  or an identical-retry result, never a second current revision.
- **A failed write must preserve the entire previous decision.** Superseding the
  old rows and inserting the new ones happens in one transaction, so a failure
  part-way through restores the prior revision rather than leaving a gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, create_engine

from local_ledger.db import init_db
from local_ledger.models import Account
from local_ledger.tax import DocumentType, Jurisdiction, TaxWorkspaceService
from local_ledger.tax.classification import (
    ClassificationState,
    ClassificationSubject,
    ReviewConflictError,
    TaxClassificationService,
    TaxTreatment,
)
from local_ledger.tax.scope import ReviewScopeMode, WorkspaceScopeService

_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


@pytest.fixture()
def file_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A real file-backed database, so two sessions are genuinely independent."""
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tmp_path / "tax"))
    url = f"sqlite:///{(tmp_path / 'decisions.db').as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    init_db(engine)
    with Session(engine) as s:
        svc = TaxWorkspaceService(s)
        ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
        doc = svc.register_document(
            workspace_id=ws.id, file_name="d.pdf", source=_PDF,
            doc_type=DocumentType.OTHER_EVIDENCE,
        )
        prop = svc.create_rental_property(
            workspace_id=ws.id, street_address="1 A St", city="Pasadena",
            state="CA", zip_code="91101",
        )
        expense = svc.record_rental_expense(
            workspace_id=ws.id, property_id=prop.id, category="supplies",
            amount_cents=10000, source_document_id=doc.id,
        )
        svc.verify_rental_expense(expense.id)
        WorkspaceScopeService(s).configure(
            workspace_id=ws.id, mode=ReviewScopeMode.DOCUMENTS_ONLY,
            property_ids=[prop.id], properties_confirmed=True,
        )
        Account(name="Checking")
        s.add(Account(name="Savings"))
        s.commit()
        ids = (ws.id, doc.id, prop.id, expense.id)
    yield engine, ids
    engine.dispose()


def test_two_sessions_cannot_both_own_the_current_decision(file_db) -> None:  # type: ignore[no-untyped-def]
    engine, (ws_id, _doc_id, prop_id, expense_id) = file_db
    decision = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE,
        subject_id=expense_id,
        treatment=TaxTreatment.RENTAL_SUPPLIES,
        property_id=prop_id,
    )

    with Session(engine) as first:
        TaxClassificationService(first, ws_id).classify(**decision, fact="first review")

    # A second session reads the current head version, then a different review
    # lands first: the stale caller must be refused, not allowed to overwrite.
    with Session(engine) as reader:
        svc = TaxClassificationService(reader, ws_id)
        head = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        stale_version = head.version

    with Session(engine) as second:
        TaxClassificationService(second, ws_id).classify(
            **decision, fact="the newer, correct review"
        )

    with Session(engine) as late:
        svc = TaxClassificationService(late, ws_id)
        with pytest.raises(ReviewConflictError):
            svc.classify(
                **decision, fact="a stale in-flight review",
                expected_head_version=stale_version,
            )
        current = svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        assert len(current) == 1
        assert current[0].fact == "the newer, correct review"


def test_the_database_rejects_a_second_head_for_one_subject(file_db) -> None:  # type: ignore[no-untyped-def]
    """The unique constraint is the last line of defence, so it must actually hold."""
    from sqlalchemy.exc import IntegrityError

    from local_ledger.tax.classification import DecisionHead

    engine, (ws_id, _doc_id, prop_id, expense_id) = file_db
    with Session(engine) as s:
        TaxClassificationService(s, ws_id).classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense_id,
            treatment=TaxTreatment.RENTAL_SUPPLIES, fact="x", property_id=prop_id,
        )
        head = TaxClassificationService(s, ws_id).head_for(
            ClassificationSubject.RENTAL_EXPENSE, expense_id
        )
        s.add(
            DecisionHead(
                workspace_id=ws_id,
                subject_type=ClassificationSubject.RENTAL_EXPENSE,
                subject_id=expense_id,
                measure=head.measure,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


def test_an_identical_retry_writes_nothing(file_db) -> None:  # type: ignore[no-untyped-def]
    engine, (ws_id, _doc_id, prop_id, expense_id) = file_db
    decision = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense_id,
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="stable rationale",
        property_id=prop_id,
    )
    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        first = svc.classify(**decision)
        head_before = svc.head_for(
            ClassificationSubject.RENTAL_EXPENSE, expense_id
        ).version

    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        second = svc.classify(**decision)
        head_after = svc.head_for(
            ClassificationSubject.RENTAL_EXPENSE, expense_id
        ).version

    assert [p.id for p in first] == [p.id for p in second], "a retry returns the same revision"
    assert head_after == head_before, "a retry must not bump the head version"


def test_a_failed_write_preserves_the_previous_decision(file_db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Failure injection: the old revision must come back whole, with its head."""
    from sqlalchemy.exc import OperationalError

    engine, (ws_id, _doc_id, prop_id, expense_id) = file_db
    decision = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense_id,
        property_id=prop_id,
    )
    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        svc.classify(**decision, treatment=TaxTreatment.RENTAL_SUPPLIES, fact="the original")
        original = svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        head_before = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, expense_id)

    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        real_flush = s.flush

        def exploding_flush(*args, **kwargs):  # type: ignore[no-untyped-def]
            real_flush(*args, **kwargs)
            raise OperationalError("stmt", {}, Exception("database is locked"))

        monkeypatch.setattr(s, "flush", exploding_flush)
        with pytest.raises(OperationalError):
            svc.classify(
                **decision, treatment=TaxTreatment.RENTAL_REPAIRS, fact="a replacement",
                authority_citation="Pub 527",
            )
        s.rollback()

    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        current = svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        head_after = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        assert [p.id for p in current] == [p.id for p in original]
        assert current[0].treatment == TaxTreatment.RENTAL_SUPPLIES
        assert current[0].state == ClassificationState.CLASSIFIED
        assert head_after.version == head_before.version
        assert head_after.current_revision == head_before.current_revision


def test_history_survives_but_never_counts(file_db) -> None:  # type: ignore[no-untyped-def]
    """A superseded decision is retained and readable, and absent from totals."""
    from local_ledger.tax.reconciliation import ReconciliationService

    engine, (ws_id, _doc_id, prop_id, expense_id) = file_db
    decision = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense_id,
        property_id=prop_id, authority_citation="Pub 527",
    )
    with Session(engine) as s:
        svc = TaxClassificationService(s, ws_id)
        svc.classify(**decision, treatment=TaxTreatment.RENTAL_REPAIRS, fact="first")
        svc.classify(**decision, treatment=TaxTreatment.RENTAL_REPAIRS, fact="second, corrected")

        history = svc.history_for(ClassificationSubject.RENTAL_EXPENSE, expense_id)
        assert len(history) == 2
        assert {h.state for h in history} == {
            ClassificationState.CLASSIFIED,
            ClassificationState.SUPERSEDED,
        }
        assert any("first" in h.fact for h in history), "the prior rationale is readable"

        report = ReconciliationService(s, ws_id).rental_totals(prop_id)
        total = next(
            t for t in report.totals
            if t.field_key == f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop_id}"
        )
        assert total.amount_cents == 10000, "history must not be counted twice"
