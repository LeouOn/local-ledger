"""Import service: stage parsed rows → dedup-classify → confirm → atomic commit.

Staged rows carry their dedup classification. ``commit`` writes only the IDs the
caller passes, in a single transaction, so a mid-commit failure leaves zero
rows. FUZZY rows sit in the review queue until the user explicitly accepts them
into the commit set.
"""

from __future__ import annotations

import uuid

from sqlmodel import Session, select

from local_ledger.db import transaction_with_retry
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ledger import audit
from local_ledger.ledger.dedup import Candidate, classify
from local_ledger.models import DedupStatus, ReviewState, StagedTransaction, Transaction


class ImportService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def stage(
        self,
        parsed_rows: list[ParsedTransaction],
        account_id: int,
        batch_label: str,
    ) -> list[StagedTransaction]:
        existing = list(
            self.session.exec(select(Transaction).where(Transaction.account_id == account_id)).all()
        )
        batch = batch_label or uuid.uuid4().hex
        staged: list[StagedTransaction] = []
        for row in parsed_rows:
            candidate = Candidate(
                account_id=account_id,
                date=row.date,
                amount_cents=row.amount_cents,
                payee=row.payee_raw,
                imported_id=row.imported_id,
            )
            status = classify(candidate, existing)
            staged.append(
                StagedTransaction(
                    import_batch=batch,
                    account_id=account_id,
                    date=row.date,
                    amount_cents=row.amount_cents,
                    payee_raw=row.payee_raw,
                    imported_id=row.imported_id,
                    dedup_status=status,
                    review_state=ReviewState.PENDING,
                )
            )
        self.session.add_all(staged)
        self.session.commit()
        for s in staged:
            self.session.refresh(s)
        return staged

    def set_review(self, staged_id: int, state: ReviewState) -> StagedTransaction:
        row = self.session.get(StagedTransaction, staged_id)
        if row is None:
            raise ValueError(f"no staged transaction {staged_id}")
        row.review_state = state
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def commit(self, staged_ids: list[int], *, actor: str) -> list[Transaction]:
        def _op() -> list[Transaction]:
            created: list[Transaction] = []
            for sid in staged_ids:
                staged = self.session.get(StagedTransaction, sid)
                if staged is None:
                    raise ValueError(f"no staged transaction {sid}")
                if staged.dedup_status == DedupStatus.EXACT:
                    continue  # auto-skip exact duplicates
                tx = Transaction(
                    account_id=staged.account_id,
                    date=staged.date,
                    amount_cents=staged.amount_cents,
                    imported_id=staged.imported_id,
                    created_by=actor,
                )
                self.session.add(tx)
                self.session.flush()
                audit.record(
                    self.session,
                    entity_type="transaction",
                    entity_id=tx.id,
                    action="create",
                    actor=actor,
                    after=tx,
                )
                created.append(tx)
            self.session.commit()
            return created

        return transaction_with_retry(_op)
