"""Entity factories used across tests. Thin helpers over the session."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryGroup,
    Payee,
    Transaction,
)


def make_account(session: Session, name: str = "Checking", type: str | AccountType = "checking") -> Account:
    acc = Account(name=name, type=AccountType(type) if isinstance(type, str) else type)
    session.add(acc)
    session.flush()
    return acc


def make_payee(session: Session, name: str = "Coffee Shop") -> Payee:
    payee = Payee(name=name, normalized_name=name.lower().strip())
    session.add(payee)
    session.flush()
    return payee


def make_category(session: Session, name: str = "Groceries", is_income: bool = False) -> Category:
    group = CategoryGroup(name="Default")
    session.add(group)
    session.flush()
    cat = Category(group_id=group.id, name=name, is_income=is_income)
    session.add(cat)
    session.flush()
    return cat


def make_transaction(
    session: Session,
    account_id: int,
    amount_cents: int = -1500,
    date: str = "2026-01-15",
    payee_id: int | None = None,
    category_id: int | None = None,
    imported_id: str | None = None,
    created_by: str = "user",
) -> Transaction:
    tx = Transaction(
        account_id=account_id,
        date=date,
        amount_cents=amount_cents,
        payee_id=payee_id,
        category_id=category_id,
        imported_id=imported_id,
        created_by=created_by,
    )
    session.add(tx)
    session.flush()
    return tx


def count(session: Session, model: type) -> int:
    return len(list(session.exec(select(model)).all()))

