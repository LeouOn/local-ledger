"""SQLite WAL concurrency stress: 2 threads x 5 inserts, zero lock errors."""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlmodel import Session

from local_ledger.db import enable_wal, init_db, transaction_with_retry
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Transaction
from tests.factories import make_account


def _fresh_engine(db_path: Path):
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    enable_wal(engine)
    init_db(engine)
    return engine


def test_wal_concurrent_inserts_complete_without_locks(tmp_path: Path) -> None:
    db = tmp_path / "stress.db"
    engine = _fresh_engine(db)

    # seed an account
    with Session(engine) as s:
        acc = make_account(s)
        s.commit()
        acc_id = acc.id

    errors: list[Exception] = []

    def worker(thread_id: int) -> None:
        with Session(engine) as s:
            svc = LedgerService(s)
            for i in range(5):
                time.sleep(random.uniform(0, 0.05))
                try:
                    svc.create_transaction(
                        Transaction(
                            account_id=acc_id, date="2026-01-10", amount_cents=-(thread_id * 100 + i + 1)
                        ),
                        actor=f"thread:{thread_id}",
                    )
                except Exception as exc:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with Session(engine) as s:
        from sqlmodel import select

        count = len(list(s.exec(select(Transaction)).all()))

    assert errors == [], f"lock errors surfaced: {errors}"
    assert count == 10, f"expected 10 rows, got {count}"


def test_wal_pragma_is_set(tmp_path: Path) -> None:
    engine = _fresh_engine(tmp_path / "pragma.db")
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"


def test_retry_helper_is_the_lock_guard() -> None:
    """The teeth: the retry helper exists and is what swallows transient locks."""
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    def op() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return "ok"

    assert transaction_with_retry(op, retries=3, delays=(0, 0, 0)) == "ok"
    assert calls["n"] == 2
