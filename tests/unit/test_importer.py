"""Import service tests: stage→dedup→confirm→atomic commit."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import ImportService
from local_ledger.models import AuditLog, StagedTransaction, Transaction
from tests.factories import make_account, make_transaction


@pytest.fixture()
def importer(session: Session, engine) -> ImportService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return ImportService(session)


def _parsed(date="2026-01-10", amount=-1500, payee="Coffee", imported_id=None):  # type: ignore[no-untyped-def]
    return ParsedTransaction(date=date, amount_cents=amount, payee_raw=payee, imported_id=imported_id)


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


def test_stage_marks_fuzzy_on_matching_amount_date_payee(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10", imported_id=None)
    session.commit()
    # notes carries the payee string for the dedup engine
    session.exec(select(Transaction)).first().notes = "Coffee"  # type: ignore[union-attr]
    session.commit()

    staged = importer.stage([_parsed(date="2026-01-12", amount=-1500, payee="coffee")], acc.id, "batch-3")
    assert staged[0].dedup_status == "FUZZY"


def test_commit_writes_transactions_and_audit_atomically(
    importer: ImportService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-01-11")], acc.id, "batch-4")

    importer.commit([s.id for s in staged], actor="import:bank_a.csv")

    assert len(list(session.exec(select(Transaction)).all())) == 2
    audits = list(session.exec(select(AuditLog)).all())
    assert len(audits) == 2
    assert all(a.actor == "import:bank_a.csv" for a in audits)
    assert all(a.action == "create" for a in audits)


def test_reimport_same_file_produces_zero_new(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    rows = [_parsed(imported_id="FIT-1"), _parsed(imported_id="FIT-2")]
    staged1 = importer.stage(rows, acc.id, "batch-5")
    importer.commit([s.id for s in staged1], actor="import:b")

    staged2 = importer.stage(rows, acc.id, "batch-6")
    statuses = {s.dedup_status for s in staged2}
    assert statuses == {"EXACT"}
    # committing the re-staged (EXACT) rows produces no new transactions
    before = len(list(session.exec(select(Transaction)).all()))
    importer.commit([s.id for s in staged2], actor="import:b")
    after = len(list(session.exec(select(Transaction)).all()))
    assert before == after == 2


def test_commit_is_atomic_on_failure(
    importer: ImportService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = make_account(session)
    session.commit()
    staged = importer.stage([_parsed(), _parsed(date="2026-01-11")], acc.id, "batch-7")

    def exploding_commit(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(session, "commit", exploding_commit)
    with pytest.raises(OperationalError):
        importer.commit([s.id for s in staged], actor="import:b")
    monkeypatch.setattr(session, "commit", lambda *a, **k: None)
    session.rollback()
    assert len(list(session.exec(select(Transaction)).all())) == 0


def test_fuzzy_rows_stay_in_review_queue_not_ledger(importer: ImportService, session: Session) -> None:
    acc = make_account(session)
    existing = make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-10")
    existing.notes = "Coffee"
    session.commit()

    staged = importer.stage(
        [_parsed(date="2026-01-12", amount=-1500, payee="coffee"), _parsed(date="2026-02-01", amount=-9999, payee="Other")],
        acc.id,
        "batch-8",
    )
    new_ids = [s.id for s in staged if s.dedup_status == "NEW"]

    # user commits only the NEW row; FUZZY stays staged
    importer.commit(new_ids, actor="import:b")
    txns = list(session.exec(select(Transaction)).all())
    assert len(txns) == 1 + 1  # the pre-existing one + the NEW one
    # the FUZZY staged row still exists, uncommitted
    remaining_fuzzy = [
        s for s in session.exec(select(StagedTransaction)).all() if s.dedup_status == "FUZZY"
    ]
    assert len(remaining_fuzzy) == 1
