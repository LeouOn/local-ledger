"""Import service tests: stage → dedup → confirm → atomic, idempotent commit.

The contract these tests pin down (TAX-01):

- nothing commits without an explicit acceptance;
- a REJECTED row never commits, whatever the caller passes;
- committing twice, or committing a batch of ids twice, writes once;
- a same-batch duplicate is a review question (FUZZY), never a silent skip, and
  two genuine equal-valued purchases both survive review;
- payee, category and source row/notes provenance survive the trip;
- a failed commit leaves neither a transaction nor an audit row.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from local_ledger.db import enable_wal, init_db
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import (
    SKIP_ALREADY_COMMITTED,
    SKIP_DUPLICATE_EXACT,
    SKIP_NEEDS_ACCEPTANCE,
    SKIP_REJECTED,
    ImportScopeError,
    ImportService,
)
from local_ledger.models import AuditLog, DedupStatus, ReviewState, StagedTransaction, Transaction
from tests.factories import make_account, make_category, make_payee, make_transaction


@pytest.fixture()
def importer(session: Session, engine) -> ImportService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return ImportService(session)


def _parsed(date="2026-01-10", amount=-1500, payee="Coffee", imported_id=None, **kw):  # type: ignore[no-untyped-def]
    return ParsedTransaction(date=date, amount_cents=amount, payee_raw=payee, imported_id=imported_id, **kw)


def _txns(session: Session) -> list[Transaction]:  # type: ignore[no-untyped-def]
    return list(session.exec(select(Transaction)).all())


# --- staging: classification ---


def test_stage_classifies_new_rows(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-01-11", amount=-2000)], acc.id, "batch-1")
    assert len(staged) == 2
    assert all(s.dedup_status == "NEW" for s in staged)
    assert all(s.review_state == "PENDING" for s in staged)


def test_stage_marks_exact_on_matching_imported_id(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10", imported_id="FIT-1")
    session.commit()

    staged = importer.stage([_parsed(imported_id="FIT-1")], acc.id, "batch-2")
    assert staged[0].dedup_status == "EXACT"


def test_stage_marks_fuzzy_via_payee_foreign_key(importer: ImportService, session: Session) -> None:
    """The payee is resolved through Transaction.payee_id, not through notes."""
    acc = make_account(session)
    payee = make_payee(session, name="Coffee Shop")
    make_transaction(
        session, acc.id, amount_cents=-1500, date="2026-01-10", payee_id=payee.id
    )
    session.commit()

    staged = importer.stage(
        [_parsed(date="2026-01-12", payee="coffee shop")], acc.id, "batch-3"
    )
    assert staged[0].dedup_status == "FUZZY"


def test_stage_does_not_treat_a_note_as_a_payee(importer: ImportService, session: Session) -> None:
    """A payee-looking note is not identity: the row stays NEW, not FUZZY."""
    acc = make_account(session)
    tx = make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10")
    tx.notes = "coffee"
    session.commit()

    staged = importer.stage([_parsed(date="2026-01-12", payee="Coffee")], acc.id, "batch-3b")
    assert staged[0].dedup_status == "NEW"


def test_stage_preserves_payee_notes_and_source_row(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage(
        [_parsed(notes="split payment", source_row=7)], acc.id, "batch-prov"
    )
    row = staged[0]
    assert row.payee_raw == "Coffee"
    assert row.notes == "split payment"
    assert row.source_row == 7
    assert row.import_batch == "batch-prov"


# --- staging: same-batch and cross-batch duplicates ---


def test_same_batch_duplicate_is_fuzzy_not_exact(importer: ImportService, session: Session) -> None:
    """An in-file duplicate must reach review; auto-skip would lose a real purchase."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed()], acc.id, "batch-4")
    assert [s.dedup_status for s in staged] == [DedupStatus.NEW, DedupStatus.FUZZY]


def test_second_batch_sees_uncommitted_rows_as_fuzzy(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    first = importer.stage([_parsed()], acc.id, "batch-5")
    second = importer.stage([_parsed()], acc.id, "batch-6")
    assert first[0].dedup_status == "NEW"
    assert second[0].dedup_status == "FUZZY"


def test_equal_valued_purchases_both_survive_review(importer: ImportService, session: Session) -> None:
    """Two genuine identical purchases are not silently lost by dedup."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed()], acc.id, "batch-7")
    importer.accept([s.id for s in staged])
    result = importer.commit([s.id for s in staged], actor="import:bank.csv")
    assert len(result.created) == 2
    assert len(_txns(session)) == 2


# --- commit: review enforcement ---


def test_pending_rows_are_not_committed_without_acceptance(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-8")

    result = importer.commit([staged[0].id], actor="import:b")

    assert result.created == []
    assert result.skipped_reasons == {staged[0].id: SKIP_NEEDS_ACCEPTANCE}
    assert _txns(session) == []


def test_pending_fuzzy_row_is_not_committed_without_acceptance(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    payee = make_payee(session, name="Coffee Shop")
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10", payee_id=payee.id)
    session.commit()
    staged = importer.stage([_parsed(date="2026-01-12", payee="coffee shop")], acc.id, "batch-9")
    assert staged[0].dedup_status == "FUZZY"

    result = importer.commit([staged[0].id], actor="import:b")

    assert result.skipped_reasons == {staged[0].id: SKIP_NEEDS_ACCEPTANCE}
    assert len(_txns(session)) == 1  # only the pre-existing row


def test_accepted_fuzzy_row_commits(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    payee = make_payee(session, name="Coffee Shop")
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10", payee_id=payee.id)
    session.commit()
    staged = importer.stage([_parsed(date="2026-01-12", payee="coffee shop")], acc.id, "batch-10")
    importer.accept([staged[0].id])

    result = importer.commit([staged[0].id], actor="import:b")

    assert len(result.created) == 1
    assert len(_txns(session)) == 2


def test_rejected_row_is_never_committed(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-11")
    importer.reject([staged[0].id])

    result = importer.commit([staged[0].id], actor="import:b")

    assert result.created == []
    assert result.skipped_reasons == {staged[0].id: SKIP_REJECTED}
    assert _txns(session) == []


def test_rejection_is_terminal(importer: ImportService, session: Session) -> None:
    """A rejected row cannot be revived by a later accept, so 'never' is unconditional."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-11c")
    importer.reject([staged[0].id])

    with pytest.raises(ValueError):
        importer.accept([staged[0].id])

    result = importer.commit([staged[0].id], actor="import:b")
    assert result.created == []
    assert _txns(session) == []


def test_rejected_row_stays_rejected_even_if_accept_came_first(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-02-02")], acc.id, "batch-11b")
    ids = [s.id for s in staged]
    importer.accept(ids)
    importer.reject([ids[0]])

    result = importer.commit(ids, actor="import:b")

    assert result.skipped_reasons == {ids[0]: SKIP_REJECTED}
    assert len(result.created) == 1


# --- commit: idempotency ---


def test_repeated_commit_of_same_ids_writes_once(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-12")
    importer.accept([staged[0].id])

    first = importer.commit([staged[0].id], actor="import:b")
    second = importer.commit([staged[0].id], actor="import:b")

    assert len(first.created) == 1
    assert second.created == []
    assert second.skipped_reasons == {staged[0].id: SKIP_ALREADY_COMMITTED}
    assert len(_txns(session)) == 1


def test_repeated_ids_in_one_commit_write_once(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-13")
    sid = staged[0].id
    importer.accept([sid])

    result = importer.commit([sid, sid, sid], actor="import:b")

    assert len(result.created) == 1
    assert len(_txns(session)) == 1


def test_staged_row_records_the_transaction_it_produced(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-14")
    importer.accept([staged[0].id])

    result = importer.commit([staged[0].id], actor="import:bank.csv")

    session.refresh(staged[0])
    assert staged[0].committed_transaction_id == result.created[0].id
    assert staged[0].committed_by == "import:bank.csv"
    assert staged[0].committed_at is not None


def test_reimport_with_external_ids_produces_no_new_rows(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    rows = [_parsed(imported_id="FIT-1"), _parsed(imported_id="FIT-2")]
    first = importer.stage(rows, acc.id, "batch-15")
    importer.accept([s.id for s in first])
    importer.commit([s.id for s in first], actor="import:b")

    second = importer.stage(rows, acc.id, "batch-16")
    assert {s.dedup_status for s in second} == {DedupStatus.EXACT}
    result = importer.commit([s.id for s in second], actor="import:b")

    assert result.created == []
    assert len(_txns(session)) == 2


def test_reimport_without_external_ids_asks_instead_of_writing(
    importer: ImportService, session: Session
) -> None:
    """No external id means no proof of identity: FUZZY, and unconfirmed it stays out."""
    acc = make_account(session)
    session.commit()
    rows = [_parsed()]
    first = importer.stage(rows, acc.id, "batch-17")
    importer.accept([s.id for s in first])
    importer.commit([s.id for s in first], actor="import:b")

    second = importer.stage(rows, acc.id, "batch-18")
    assert second[0].dedup_status == "FUZZY"
    result = importer.commit([second[0].id], actor="import:b")

    assert result.skipped_reasons == {second[0].id: SKIP_NEEDS_ACCEPTANCE}
    assert len(_txns(session)) == 1


def test_commit_rechecks_conflicts_against_the_live_ledger(
    importer: ImportService, session: Session
) -> None:
    """A row that became an exact duplicate after staging is skipped, not written."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(imported_id="FIT-9")], acc.id, "batch-19")
    importer.accept([staged[0].id])
    # Another path wrote the same source row id into the ledger meanwhile.
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10", imported_id="FIT-9")
    session.commit()

    result = importer.commit([staged[0].id], actor="import:b")

    assert result.created == []
    assert result.skipped_reasons == {staged[0].id: SKIP_DUPLICATE_EXACT}
    assert len(_txns(session)) == 1


# --- commit: scoping ---


def test_commit_rejects_rows_from_another_batch(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    a = importer.stage([_parsed()], acc.id, "batch-a")
    b = importer.stage([_parsed(date="2026-03-03")], acc.id, "batch-b")
    importer.accept([a[0].id, b[0].id])

    with pytest.raises(ImportScopeError):
        importer.commit([a[0].id, b[0].id], actor="import:b", batch="batch-a")

    assert _txns(session) == []


def test_commit_rejects_rows_from_another_account(
    importer: ImportService, session: Session
) -> None:
    first = make_account(session, name="Checking")
    second = make_account(session, name="Savings")
    session.commit()
    staged = importer.stage([_parsed()], second.id, "batch-c")
    importer.accept([staged[0].id])

    with pytest.raises(ImportScopeError):
        importer.commit([staged[0].id], actor="import:b", account_id=first.id)

    assert _txns(session) == []


def test_commit_rejects_an_unknown_id(importer: ImportService, session: Session) -> None:
    make_account(session)
    session.commit()
    with pytest.raises(ImportScopeError):
        importer.commit([4242], actor="import:b")


# --- commit: payload, audit, rollback ---


def test_commit_writes_transactions_and_audit_atomically(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-01-11")], acc.id, "batch-20")
    ids = [s.id for s in staged]
    importer.accept(ids)

    importer.commit(ids, actor="import:bank_a.csv")

    assert len(_txns(session)) == 2
    audits = list(session.exec(select(AuditLog)).all())
    assert len(audits) == 2
    assert all(a.actor == "import:bank_a.csv" for a in audits)
    assert all(a.action == "create" for a in audits)


def test_commit_retains_payee_and_approved_category(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    payee = make_payee(session, name="Coffee Shop")
    cat = make_category(session, name="Dining")
    session.commit()
    staged = importer.stage([_parsed(payee="Coffee Shop")], acc.id, "batch-21")
    staged[0].suggested_category_id = cat.id
    session.add(staged[0])
    session.commit()
    importer.accept([staged[0].id])

    importer.commit([staged[0].id], actor="import:b")

    tx = _txns(session)[0]
    assert tx.payee_id == payee.id
    assert tx.category_id == cat.id


def test_commit_resolves_payee_name_to_a_foreign_key(
    importer: ImportService, session: Session
) -> None:
    """The payee a bank sent in the file is not lost on the way in."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(payee="Whole Foods  #4471")], acc.id, "batch-22")
    importer.accept([staged[0].id])

    importer.commit([staged[0].id], actor="import:b")

    tx = _txns(session)[0]
    assert tx.payee_id is not None
    from local_ledger.models import Payee

    payee = session.get(Payee, tx.payee_id)
    assert payee is not None
    assert payee.name == "Whole Foods  #4471"


def test_commit_is_atomic_on_failure(
    importer: ImportService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-01-11")], acc.id, "batch-23")
    ids = [s.id for s in staged]
    importer.accept(ids)

    def exploding_commit(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(session, "commit", exploding_commit)
    with pytest.raises(OperationalError):
        importer.commit(ids, actor="import:b")
    monkeypatch.undo()
    session.rollback()

    assert _txns(session) == []
    assert list(session.exec(select(AuditLog)).all()) == []


def test_rollback_also_clears_the_staged_link(importer: ImportService, session: Session) -> None:
    """A failed commit must not leave a staged row pointing at a phantom transaction."""
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-24")
    importer.accept([staged[0].id])
    original_commit = session.commit
    calls = {"n": 0}

    def failing_commit(*a, **k):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return original_commit(*a, **k)

    session.commit = failing_commit  # type: ignore[method-assign]
    result = importer.commit([staged[0].id], actor="import:b")

    assert len(result.created) == 1
    session.refresh(staged[0])
    assert staged[0].committed_transaction_id == result.created[0].id
    assert len(_txns(session)) == 1


# --- review queue ---


def test_list_staged_filters_by_batch_and_account(importer: ImportService, session: Session) -> None:
    checking = make_account(session, name="Checking")
    savings = make_account(session, name="Savings")
    session.commit()
    importer.stage([_parsed()], checking.id, "batch-x")
    importer.stage([_parsed()], savings.id, "batch-y")

    assert len(importer.list_staged(account_id=checking.id)) == 1
    assert len(importer.list_staged(batch="batch-y")) == 1
    assert len(importer.list_staged()) == 2


def test_list_staged_hides_committed_rows(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed()], acc.id, "batch-z")
    importer.accept([staged[0].id])
    importer.commit([staged[0].id], actor="import:b")

    assert importer.list_staged() == []
    assert len(importer.list_staged(uncommitted_only=False)) == 1


def test_accept_and_reject_set_state_in_bulk(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-02-02")], acc.id, "batch-ab")
    ids = [s.id for s in staged]

    importer.accept([ids[0]])
    importer.reject([ids[1]])

    rows = {s.id: s for s in importer.list_staged(uncommitted_only=False)}
    assert rows[ids[0]].review_state is ReviewState.ACCEPTED
    assert rows[ids[1]].review_state is ReviewState.REJECTED


def test_set_review_rejects_unknown_ids(importer: ImportService, session: Session) -> None:
    with pytest.raises(ValueError):
        importer.accept([9999])


# --- concurrency ---


def test_two_sessions_committing_the_same_row_write_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlmodel import Session as SMSession
    from sqlmodel import create_engine

    db = tmp_path / "concurrent.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}", connect_args={"check_same_thread": False})
    enable_wal(engine)
    init_db(engine)

    with SMSession(engine) as s:
        acc = make_account(s)
        s.commit()
        account_id = acc.id
        staged = ImportService(s).stage([_parsed(imported_id="FIT-77")], account_id, "batch-cc")
        s.commit()
        sid = staged[0].id
        ImportService(s).accept([sid])

    results: list[object] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        with SMSession(engine) as s:
            svc = ImportService(s)
            try:
                barrier.wait(timeout=5)
                results.append(svc.commit([sid], actor=f"import:t{threading.get_ident()}"))
            except Exception as exc:  # pragma: no cover - only on a real defect
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    engine.dispose()
    assert errors == [], f"concurrent commits raised: {errors}"

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with SMSession(engine) as s:
            rows = list(s.exec(select(Transaction)).all())
            staged_rows = list(s.exec(select(StagedTransaction)).all())
    finally:
        engine.dispose()

    assert len(rows) == 1, "a staged row must produce exactly one transaction"
    assert staged_rows[0].committed_transaction_id == rows[0].id
    committed = [r for r in results if getattr(r, "created", None)]
    assert len(committed) <= 1
