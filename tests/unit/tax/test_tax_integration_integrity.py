"""Integration regressions using synthetic evidence and independent sessions."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session, create_engine

from local_ledger.db import init_db
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import ImportService
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Transaction
from local_ledger.tax import Jurisdiction, TaxWorkspaceService
from local_ledger.tax.classification import (
    ClassificationError,
    ClassificationSubject,
    ReviewConflictError,
    TaxClassificationService,
    TaxTreatment,
)
from local_ledger.tax.models import (
    EvidenceStatus,
    ReconciliationStatus,
    RentalExpenseRecord,
)
from local_ledger.tax.reconciliation import ReconciliationService, RunState, TotalBasis
from local_ledger.tax.scope import ReviewScopeMode, WorkspaceScopeService
from local_ledger.tax.service import PacketReadinessError


@pytest.fixture()
def review(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tmp_path / "evidence"))
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    init_db(engine)
    with Session(engine) as session:
        svc = TaxWorkspaceService(session)
        ws = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
        account = LedgerService(session).create_account(name="Selected")
        other = LedgerService(session).create_account(name="Other")
        doc = svc.register_document(ws.id, "synthetic.pdf", b"synthetic evidence")
        properties = [
            svc.create_rental_property(
                workspace_id=ws.id,
                street_address=f"{n} Test St",
                city="Test",
                state="CA",
                zip_code="90001",
            )
            for n in (1, 2)
        ]
        expenses = [
            svc.record_rental_expense(
                workspace_id=ws.id,
                property_id=prop.id,
                category="supplies",
                amount_cents=10000,
                source_document_id=doc.id,
            )
            for prop in properties
        ]
        for expense in expenses:
            svc.verify_rental_expense(expense.id)
        ids = (
            ws.id,
            account.id,
            other.id,
            doc.id,
            [p.id for p in properties],
            [e.id for e in expenses],
        )
    yield engine, ids
    engine.dispose()


def configure(session, ids, properties=None):  # type: ignore[no-untyped-def]
    ws, account, _, _, props, _ = ids
    return WorkspaceScopeService(session).configure(
        workspace_id=ws,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[account],
        property_ids=props if properties is None else properties,
        properties_confirmed=True,
    )


def decision(ids, index=0):  # type: ignore[no-untyped-def]
    return dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE,
        subject_id=ids[5][index],
        property_id=ids[4][index],
        treatment=TaxTreatment.RENTAL_SUPPLIES,
    )


def test_cached_session_cannot_replace_newer_review(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as first, Session(engine) as second:
        svc = TaxClassificationService(first, ids[0])
        svc.classify(**decision(ids), fact="original")
        retained_head = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, ids[5][0])
        version = retained_head.version
        TaxClassificationService(second, ids[0]).classify(**decision(ids), fact="newer")
        with pytest.raises(ReviewConflictError):
            svc.classify(**decision(ids), fact="stale", expected_head_version=version)
        current = svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, ids[5][0])
        assert [p.fact for p in current] == ["newer"]
        assert (
            sum(svc.allocations_for(ClassificationSubject.RENTAL_EXPENSE, ids[5][0]).values())
            == 10000
        )


@pytest.mark.parametrize("status", [EvidenceStatus.SUPERSEDED, EvidenceStatus.REJECTED])
def test_rental_scope_excludes_unselected_property_and_history(review, status):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        cls = TaxClassificationService(session, ids[0])
        for n in (0, 1):
            cls.classify(**decision(ids, n), fact="reviewed")
        configure(session, ids, properties=[ids[4][0]])
        rec = ReconciliationService(session, ids[0])
        report = rec.rental_totals()
        assert sum(t.amount_cents for t in report.totals) == 10000
        with pytest.raises(ClassificationError, match="scope"):
            cls.classify(**decision(ids, 1), fact="outside scope")
        session.get(RentalExpenseRecord, ids[5][0]).status = status
        session.commit()
        assert rec.rental_totals().unresolved_count == 0


def complete_review(session, ids):  # type: ignore[no-untyped-def]
    configure(session, ids)
    cls = TaxClassificationService(session, ids[0])
    for n in (0, 1):
        cls.classify(**decision(ids, n), fact="reviewed")
    svc = TaxWorkspaceService(session)
    comparison = svc.record_reconciliation_item(
        workspace_id=ids[0],
        tax_section="Schedule E",
        field_key="supplies",
        source_working_paper_cents=20000,
        taxact_entered_cents=20000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
    )
    return svc, comparison


@pytest.mark.parametrize(
    "status,entered,delta",
    [
        (ReconciliationStatus.UNRESOLVED, 20000, 0),
        (ReconciliationStatus.STALE, 20000, 0),
        (ReconciliationStatus.DISCREPANCY, 19000, 1000),
        (ReconciliationStatus.MATCHED, 19000, 0),
    ],
)
def test_packet_refuses_unreviewed_or_inconsistent_comparisons(review, status, entered, delta):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        svc, comparison = complete_review(session, ids)
        comparison.status = status
        comparison.taxact_entered_cents = entered
        comparison.delta_cents = delta
        session.commit()
        with pytest.raises(PacketReadinessError, match="TaxAct comparison"):
            svc.create_evidence_packet(ids[0], is_draft=False)
        assert not json.loads(svc.create_evidence_packet(ids[0]).snapshot_json)["readiness"][
            "ready"
        ]


def test_new_review_replaces_stale_comparison_without_erasing_history(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        svc, old_comparison = complete_review(session, ids)
        old_packet = svc.create_evidence_packet(ids[0], is_draft=False)
        field = svc.propose_field(ids[0], ids[3], "interest", "$10", 1000)
        svc.reject_field(field.id, rejected_by="reviewer", notes="not on form")
        new_comparison = svc.record_reconciliation_item(
            workspace_id=ids[0],
            tax_section="Schedule E",
            field_key="supplies",
            source_working_paper_cents=20000,
            taxact_entered_cents=20000,
            delta_cents=0,
            status=ReconciliationStatus.MATCHED,
        )
        assert old_comparison.status == ReconciliationStatus.STALE
        new_packet = svc.create_evidence_packet(ids[0], is_draft=False)
        assert new_comparison.id != old_comparison.id
        assert len(json.loads(new_packet.snapshot_json)["comparisons"]) == 2
        assert not json.loads(old_packet.snapshot_json)["verified_fields"]


def test_corrected_document_missing_replacement_is_a_separate_gap(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        svc, _ = complete_review(session, ids)
        svc.register_document(
            ids[0],
            "corrected.pdf",
            b"different synthetic evidence",
            is_corrected=True,
            supersedes_doc_id=ids[3],
        )
        report = ReconciliationService(session, ids[0]).source_form_totals()
        assert report.unresolved_count == 0
        with pytest.raises(PacketReadinessError, match="no replacement evidence"):
            svc.create_evidence_packet(ids[0], is_draft=False)


def test_readiness_rereads_cached_sources_and_comparisons(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as first, Session(engine) as second:
        svc, retained_comparison = complete_review(first, ids)
        retained_source = first.get(RentalExpenseRecord, ids[5][0])
        source = second.get(RentalExpenseRecord, retained_source.id)
        source.amount_cents = 15000
        second.commit()
        assert retained_comparison.status == ReconciliationStatus.MATCHED
        with pytest.raises(PacketReadinessError, match="unresolved rental"):
            svc.create_evidence_packet(ids[0], is_draft=False)
        assert retained_source.amount_cents == 15000


def test_readiness_refuses_a_comparison_changed_in_another_session(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as first, Session(engine) as second:
        svc, cached = complete_review(first, ids)
        newer = second.get(type(cached), cached.id)
        newer.status = ReconciliationStatus.DISCREPANCY
        second.commit()
        assert cached.status == ReconciliationStatus.MATCHED
        with pytest.raises(PacketReadinessError, match="not currently matched"):
            svc.create_evidence_packet(ids[0], is_draft=False)


def test_source_totals_reread_cached_verification_status(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as first, Session(engine) as second:
        configure(first, ids)
        svc = TaxWorkspaceService(first)
        field = svc.propose_field(ids[0], ids[3], "interest", "$10", 1000)
        svc.verify_field(field.id, verified_by="reviewer")
        newer = second.get(type(field), field.id)
        newer.status = EvidenceStatus.SUPERSEDED
        second.commit()
        report = ReconciliationService(first, ids[0]).source_form_totals()
        assert report.totals[0].amount_cents == 0
        assert report.unresolved_count == 0


def test_verified_sale_with_unknown_basis_blocks_final_packet(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        svc, _ = complete_review(session, ids)
        sale = svc.record_investment_sale(
            workspace_id=ids[0],
            source_document_id=ids[3],
            security_description="Synthetic noncovered lot",
            date_sold="2025-06-01",
            proceeds_cents=10000,
        )
        svc.verify_investment_sale(sale.id)
        with pytest.raises(PacketReadinessError, match="unresolved investment basis"):
            svc.create_evidence_packet(ids[0], is_draft=False)


@pytest.mark.parametrize(
    "date,selected,stale",
    [
        ("2025-09-09", True, True),
        ("2024-09-09", True, False),
        ("2025-09-09", False, False),
    ],
)
def test_new_ledger_row_invalidates_only_affected_scope(review, date, selected, stale):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        configure(session, ids)
        rec = ReconciliationService(session, ids[0])
        run = rec.persist(rec.compute_run(TotalBasis.BANK_FLOW))
        LedgerService(session).create_transaction(
            Transaction(
                account_id=ids[1] if selected else ids[2],
                date=date,
                amount_cents=50000,
            )
        )
        session.refresh(run)
        assert (run.state == RunState.STALE) == stale


def test_rejected_fields_are_history_not_pending(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        configure(session, ids)
        svc = TaxWorkspaceService(session)
        field = svc.propose_field(ids[0], ids[3], "interest", "$10", 1000)
        svc.reject_field(field.id, rejected_by="reviewer", notes="incorrect proposal")
        report = ReconciliationService(session, ids[0]).source_form_totals()
        assert report.unresolved_count == 0
        assert report.unresolved_cents == 0
        assert field.id in report.totals[0].excluded_ids


def test_import_commit_stales_existing_run_but_staging_and_replay_do_not(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        configure(session, ids)
        rec = ReconciliationService(session, ids[0])
        run = rec.persist(rec.compute_run(TotalBasis.BANK_FLOW))
        importer = ImportService(session)
        staged = importer.stage(
            [
                ParsedTransaction(
                    date="2025-01-01",
                    amount_cents=15000,
                    payee_raw="Synthetic rent",
                )
            ],
            ids[1],
            "test",
        )
        importer.accept(staged[0].id)
        session.refresh(run)
        assert run.state == RunState.CURRENT
        result = importer.commit([staged[0].id], actor="reviewer")
        assert len(result.created) == 1
        session.refresh(run)
        assert run.state == RunState.STALE
        replacement = rec.persist(rec.compute_run(TotalBasis.BANK_FLOW))
        assert replacement.unresolved_count == 1
        assert not importer.commit([staged[0].id], actor="reviewer").created
        session.refresh(replacement)
        assert replacement.state == RunState.CURRENT


def test_failure_after_invalidation_rolls_back_without_caller_cleanup(review, monkeypatch):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        configure(session, ids)
        svc = TaxClassificationService(session, ids[0])
        original = svc.classify(**decision(ids), fact="original")[0].id
        version = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, ids[5][0]).version
        rec = ReconciliationService(session, ids[0])
        run = rec.persist(rec.compute_run(TotalBasis.BANK_FLOW))
        packet = TaxWorkspaceService(session).create_evidence_packet(ids[0])
        real_invalidate = svc._invalidate_downstream

        def fail_after_invalidation():  # type: ignore[no-untyped-def]
            real_invalidate()
            raise RuntimeError("injected failure")

        monkeypatch.setattr(svc, "_invalidate_downstream", fail_after_invalidation)
        with pytest.raises(RuntimeError, match="injected"):
            svc.classify(**decision(ids), fact="replacement")
        assert (
            svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, ids[5][0])[0].id
            == original
        )
        assert svc.head_for(ClassificationSubject.RENTAL_EXPENSE, ids[5][0]).version == version
        session.refresh(run)
        session.refresh(packet)
        assert run.state == RunState.CURRENT
        assert not packet.is_stale


@pytest.mark.parametrize("existing", [False, True])
def test_simultaneous_writers_have_one_winner(review, monkeypatch, existing):  # type: ignore[no-untyped-def]
    engine, ids = review
    expected_version = 0
    if existing:
        with Session(engine) as session:
            svc = TaxClassificationService(session, ids[0])
            svc.classify(**decision(ids), fact="original")
            expected_version = svc.head_for(ClassificationSubject.RENTAL_EXPENSE, ids[5][0]).version
    barrier = threading.Barrier(2)
    seen = threading.local()
    real_head_for = TaxClassificationService.head_for

    def interleaved_head_for(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        head = real_head_for(self, *args, **kwargs)
        if not getattr(seen, "waited", False):
            seen.waited = True
            barrier.wait(timeout=10)
        return head

    monkeypatch.setattr(TaxClassificationService, "head_for", interleaved_head_for)

    def write(fact):  # type: ignore[no-untyped-def]
        with Session(engine) as session:
            try:
                TaxClassificationService(session, ids[0]).classify(
                    **decision(ids),
                    fact=fact,
                    expected_head_version=expected_version,
                )
                return "won"
            except ReviewConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("review A", "review B")))
    assert sorted(results) == ["conflict", "won"]
    monkeypatch.setattr(TaxClassificationService, "head_for", real_head_for)
    with Session(engine) as session:
        svc = TaxClassificationService(session, ids[0])
        parts = svc.current_revision_parts(ClassificationSubject.RENTAL_EXPENSE, ids[5][0])
        assert len(parts) == 1
        assert parts[0].allocation_bp == 10000


def test_packet_readiness_requires_scope_and_current_review(review):  # type: ignore[no-untyped-def]
    engine, ids = review
    with Session(engine) as session:
        svc = TaxWorkspaceService(session)
        with pytest.raises(PacketReadinessError, match="UNCONFIGURED"):
            svc.create_evidence_packet(ids[0], is_draft=False)
        assert svc.create_evidence_packet(ids[0]).is_draft
        configure(session, ids)
        with pytest.raises(PacketReadinessError, match="unresolved"):
            svc.create_evidence_packet(ids[0], is_draft=False)
        cls = TaxClassificationService(session, ids[0])
        for n in (0, 1):
            cls.classify(**decision(ids, n), fact="reviewed")
        svc.record_reconciliation_item(
            workspace_id=ids[0],
            tax_section="Schedule E",
            field_key="supplies",
            source_working_paper_cents=20000,
            taxact_entered_cents=20000,
            delta_cents=0,
            status=ReconciliationStatus.MATCHED,
        )
        final = svc.create_evidence_packet(ids[0], is_draft=False)
        snapshot = json.loads(final.snapshot_json)
        assert snapshot["readiness"]["ready"]
        assert snapshot["scope"]["property_ids"] == sorted(ids[4])
        assert len(snapshot["classifications"]) == 2
        svc.propose_field(ids[0], ids[3], "interest", "$10", 1000)
        session.refresh(final)
        assert final.is_stale
        with pytest.raises(PacketReadinessError, match="pending"):
            svc.create_evidence_packet(ids[0], is_draft=False)
