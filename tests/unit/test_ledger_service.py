"""Ledger service TDD tests: CRUD + audit + atomic transfers + WAL retry."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from local_ledger.db import init_db, transaction_with_retry
from local_ledger.ledger.service import LedgerService
from local_ledger.models import AuditLog, Transaction
from tests.factories import count


@pytest.fixture()
def service(session: Session, engine) -> LedgerService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return LedgerService(session)


# --- audit on every mutation ---


def test_create_account_writes_one_audit_row(service: LedgerService, session: Session) -> None:
    acc = service.create_account(name="Checking", type="checking")
    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 1
    audit = rows[0]
    assert audit.actor == "user"
    assert audit.action == "create"
    assert audit.entity_type == "account"
    assert audit.entity_id == acc.id
    assert "Checking" in (audit.after_json or "")


def test_audit_actor_records_caller(service: LedgerService, session: Session) -> None:
    service.create_account(name="A", type="cash", actor="import:bank_a.csv")
    audit = session.exec(select(AuditLog)).first()
    assert audit.actor == "import:bank_a.csv"


# --- transfer pair atomicity ---


def test_create_transfer_makes_two_linked_rows(service: LedgerService, session: Session) -> None:
    checking = service.create_account(name="Checking", type="checking")
    savings = service.create_account(name="Savings", type="savings")

    out_tx, in_tx = service.create_transfer(
        from_account_id=checking.id,
        to_account_id=savings.id,
        amount_cents=10000,
        date="2026-02-01",
    )

    assert out_tx.amount_cents == -10000
    assert in_tx.amount_cents == 10000
    assert out_tx.transferred_id == in_tx.id
    assert in_tx.transferred_id == out_tx.id
    assert count(session, Transaction) == 2


def test_create_transfer_rejects_zero_amount(service: LedgerService) -> None:
    a = service.create_account(name="A", type="checking")
    b = service.create_account(name="B", type="savings")
    with pytest.raises(ValueError):
        service.create_transfer(
            from_account_id=a.id, to_account_id=b.id, amount_cents=0, date="2026-02-01"
        )


def test_transfer_is_atomic_on_commit_failure(
    service: LedgerService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = service.create_account(name="A", type="checking")
    b = service.create_account(name="B", type="savings")

    original_commit = session.commit

    def exploding_commit(*_a: object, **_k: object) -> None:
        raise OperationalError("statement", {}, OperationalError.orig if hasattr(OperationalError, "orig") else Exception("database is locked"))  # type: ignore[attr-defined]

    monkeypatch.setattr(session, "commit", exploding_commit)
    with pytest.raises(OperationalError):
        service.create_transfer(
            from_account_id=a.id, to_account_id=b.id, amount_cents=5000, date="2026-02-01"
        )
    monkeypatch.setattr(session, "commit", original_commit)
    session.rollback()
    assert count(session, Transaction) == 0


# --- delete cascades the pair ---


def test_delete_transaction_cascades_transfer_pair(
    service: LedgerService, session: Session
) -> None:
    a = service.create_account(name="A", type="checking")
    b = service.create_account(name="B", type="savings")
    out_tx, _in_tx = service.create_transfer(
        from_account_id=a.id, to_account_id=b.id, amount_cents=7000, date="2026-02-01"
    )

    service.delete_transaction(out_tx.id)

    assert count(session, Transaction) == 0


# --- retry-on-locked ---


def test_transaction_with_retry_retries_on_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr("local_ledger.db.time.sleep", lambda _s: None)

    def op() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return "ok"

    result = transaction_with_retry(op, retries=3, delays=(0.01, 0.01, 0.01))
    assert result == "ok"
    assert calls["n"] == 3


def test_transaction_with_retry_gives_up_after_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_ledger.db.time.sleep", lambda _s: None)

    def op() -> None:
        raise OperationalError("stmt", {}, Exception("database is locked"))

    with pytest.raises(OperationalError):
        transaction_with_retry(op, retries=2, delays=(0.01, 0.01))


# --- no LLM imports in ledger layer (grep-enforced via import check) ---


def test_ledger_layer_has_no_llm_imports() -> None:
    import inspect

    import local_ledger.ledger.service as svc

    src = inspect.getsource(svc)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src, f"ledger service must not reference {token!r}"
