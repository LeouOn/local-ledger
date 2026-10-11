"""Ledger service: CRUD over accounts/payees/categories/transactions.

Invariants enforced here:
- transfer pairs are created atomically (two rows, cross-linked via
  ``transferred_id``) and deleted together;
- every mutation writes exactly one AuditLog row with an explicit ``actor``;
- writes go through ``transaction_with_retry`` so SQLite ``database is locked``
  errors back off instead of surfacing.

No LLM code lives in this module.
"""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.db import transaction_with_retry
from local_ledger.ledger import audit
from local_ledger.ledger.invalidation import notify_transactions_changed
from local_ledger.models import (
    Account,
    AccountType,
    Category,
    Payee,
    Transaction,
)


class LedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- accounts ----

    def create_account(
        self,
        *,
        name: str,
        type: str | AccountType = "checking",
        actor: str = "user",
    ) -> Account:
        acc = Account(name=name, type=AccountType(type) if isinstance(type, str) else type)

        def _op() -> Account:
            self.session.add(acc)
            self.session.flush()
            audit.record(
                self.session,
                entity_type="account",
                entity_id=acc.id,
                action="create",
                actor=actor,
                after=acc,
            )
            self.session.commit()
            return acc

        return transaction_with_retry(_op)

    # ---- payees ----

    def create_payee(self, *, name: str, actor: str = "user") -> Payee:
        payee = Payee(name=name, normalized_name=name.lower().strip())

        def _op() -> Payee:
            self.session.add(payee)
            self.session.flush()
            audit.record(
                self.session,
                entity_type="payee",
                entity_id=payee.id,
                action="create",
                actor=actor,
                after=payee,
            )
            self.session.commit()
            return payee

        return transaction_with_retry(_op)

    # ---- categories ----

    def create_category(
        self,
        *,
        name: str,
        group_id: int | None = None,
        is_income: bool = False,
        actor: str = "user",
    ) -> Category:
        cat = Category(group_id=group_id, name=name, is_income=is_income)

        def _op() -> Category:
            self.session.add(cat)
            self.session.flush()
            audit.record(
                self.session,
                entity_type="category",
                entity_id=cat.id,
                action="create",
                actor=actor,
                after=cat,
            )
            self.session.commit()
            return cat

        return transaction_with_retry(_op)

    # ---- transactions ----

    def create_transaction(self, tx: Transaction, actor: str = "user") -> Transaction:
        def _op() -> Transaction:
            self.session.add(tx)
            self.session.flush()
            tx.created_by = actor
            audit.record(
                self.session,
                entity_type="transaction",
                entity_id=tx.id,
                action="create",
                actor=actor,
                after=tx,
            )
            notify_transactions_changed(self.session, [tx.id])
            self.session.commit()
            return tx

        return transaction_with_retry(_op)

    def create_transfer(
        self,
        *,
        from_account_id: int,
        to_account_id: int,
        amount_cents: int,
        date: str,
        actor: str = "user",
    ) -> tuple[Transaction, Transaction]:
        if amount_cents == 0:
            raise ValueError("transfer amount_cents must be nonzero")
        if amount_cents < 0:
            raise ValueError("transfer amount_cents must be positive")

        out_tx = Transaction(
            account_id=from_account_id, date=date, amount_cents=-amount_cents, created_by=actor
        )
        in_tx = Transaction(
            account_id=to_account_id, date=date, amount_cents=amount_cents, created_by=actor
        )

        def _op() -> tuple[Transaction, Transaction]:
            self.session.add(out_tx)
            self.session.add(in_tx)
            self.session.flush()
            out_tx.transferred_id = in_tx.id
            in_tx.transferred_id = out_tx.id
            audit.record(
                self.session,
                entity_type="transaction",
                entity_id=out_tx.id,
                action="create",
                actor=actor,
                after=out_tx,
            )
            audit.record(
                self.session,
                entity_type="transaction",
                entity_id=in_tx.id,
                action="create",
                actor=actor,
                after=in_tx,
            )
            notify_transactions_changed(self.session, [out_tx.id, in_tx.id])
            self.session.commit()
            return out_tx, in_tx

        return transaction_with_retry(_op)

    def delete_transaction(self, transaction_id: int, actor: str = "user") -> None:
        def _op() -> None:
            tx = self.session.get(Transaction, transaction_id)
            if tx is None:
                return
            ids_to_delete = [tx.id]
            if tx.transferred_id is not None:
                mirror = self.session.get(Transaction, tx.transferred_id)
                if mirror is not None:
                    ids_to_delete.append(mirror.id)
            prior_coordinates = []
            for tid in ids_to_delete:
                victim = self.session.get(Transaction, tid)
                if victim is not None:
                    prior_coordinates.append((victim.account_id, int(victim.date[:4])))
                    audit.record(
                        self.session,
                        entity_type="transaction",
                        entity_id=tid,
                        action="delete",
                        actor=actor,
                        before=victim,
                    )
                    self.session.delete(victim)
            # Any tax comparison that consumed these rows is now comparing against
            # evidence that no longer exists. This must happen before the commit so
            # the invalidation and the deletion are atomic; without this call the
            # comparison stayed MATCHED after a real deletion.
            notify_transactions_changed(self.session, ids_to_delete, prior_account_years=prior_coordinates)
            self.session.commit()

        transaction_with_retry(_op)

    def list_transactions(self, account_id: int | None = None) -> list[Transaction]:
        stmt = select(Transaction)
        if account_id is not None:
            stmt = stmt.where(Transaction.account_id == account_id)
        return list(self.session.exec(stmt).all())
