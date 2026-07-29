"""Model invariant tests (RED first).

These pin the load-bearing invariants from the plan:
- money is integer cents; floats are rejected at construction;
- transaction dates are naive YYYY-MM-DD strings, validated;
- zero-amount transactions are rejected at the database boundary (CHECK).
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from local_ledger.models import Account, Transaction


def _valid_kwargs(**overrides: object) -> dict:
    base: dict = {
        "account_id": 1,
        "date": "2026-01-15",
        "amount_cents": -1234,
    }
    base.update(overrides)
    return base


def test_float_amount_rejected() -> None:
    """A float amount must never construct - it would silently corrupt money."""
    with pytest.raises(ValidationError):
        Transaction(**_valid_kwargs(amount_cents=12.5))


def test_float_whole_number_also_rejected() -> None:
    """Even a 'whole' float is rejected - strict integer-only money."""
    with pytest.raises(ValidationError):
        Transaction(**_valid_kwargs(amount_cents=12345.0))


def test_bad_calendar_date_rejected() -> None:
    """An impossible calendar date must fail validation, not silently store."""
    with pytest.raises(ValidationError):
        Transaction(**_valid_kwargs(date="2026-13-45"))


def test_bad_date_format_rejected() -> None:
    """Non-ISO date formats are rejected - the canonical form is YYYY-MM-DD."""
    with pytest.raises(ValidationError):
        Transaction(**_valid_kwargs(date="15/01/2026"))


def test_valid_transaction_constructs() -> None:
    """A well-formed integer-cents, ISO-date transaction constructs cleanly."""
    tx = Transaction(**_valid_kwargs(amount_cents=12345))
    assert tx.amount_cents == 12345
    assert tx.date == "2026-01-15"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", tx.date)


def test_zero_amount_rejected_at_commit(session, engine) -> None:  # type: ignore[no-untyped-def]
    """Zero-amount transactions are blocked by a DB CHECK (IntegrityError)."""
    from local_ledger.db import init_db

    init_db(engine)
    session.add(Account(name="Checking", type="checking"))
    session.commit()
    acc_id = session.exec(select(Account)).first().id  # type: ignore[union-attr]

    session.add(Transaction(account_id=acc_id, date="2026-01-15", amount_cents=0))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
