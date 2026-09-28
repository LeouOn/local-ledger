"""Import service: stage parsed rows → dedup-classify → confirm → atomic commit.

The staging queue is the only door to the ledger. A staged row keeps its
dedup classification, its review state, its source batch/row provenance and,
once written, the id of the transaction it produced.

``commit`` is the enforcement point, and it is deliberately unforgiving:

- a row already committed is skipped, so repeated clicks and replayed ids are
  no-ops rather than duplicates;
- a REJECTED row is never written, whatever the caller asks for;
- a NEW or FUZZY row must have been explicitly ACCEPTED - confirmation is the
  caller's job and is never implied by a row merely being PENDING;
- a row that has become an EXACT duplicate since staging is skipped again, so
  conflicts are re-checked against the live ledger at commit time;
- ids outside the requested account/batch scope abort the whole commit.

Skips are reported, never silent. ``CommitResult`` says which rows were written
and why each remaining row was not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from local_ledger.db import transaction_with_retry
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.payees import resolve_or_create_payee
from local_ledger.ledger import audit
from local_ledger.ledger.dedup import Candidate, classify
from local_ledger.models import (
    DedupStatus,
    Payee,
    ReviewState,
    StagedTransaction,
    Transaction,
)

# Stable skip reasons. Callers (and tests) match on these, so treat them as API.
SKIP_ALREADY_COMMITTED = "already-committed"
SKIP_REJECTED = "rejected"
SKIP_NEEDS_ACCEPTANCE = "needs-acceptance"
SKIP_DUPLICATE_EXACT = "duplicate-exact"
SKIP_OUT_OF_SCOPE = "out-of-scope"

# Compare-and-set claim on a staged row. The WHERE clause is the idempotency
# guarantee: exactly one writer can move a row out of the uncommitted state, so
# two sessions committing overlapping rows cannot both win. The unique index on
# transaction(account_id, imported_id) is the second line of defence.
_CLAIM = text(
    "UPDATE stagedtransaction SET committed_transaction_id = :tx_id, committed_at = :at, "
    "committed_by = :actor WHERE id = :sid AND committed_transaction_id IS NULL"
).bindparams(bindparam("at", type_=DateTime))


@dataclass(frozen=True)
class SkippedRow:
    staged_id: int
    reason: str


@dataclass(frozen=True)
class CommitResult:
    created: list[Transaction] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)

    @property
    def committed_ids(self) -> list[int]:
        return [s.id for s in self.created if s.id is not None]

    @property
    def skipped_reasons(self) -> dict[int, str]:
        return {s.staged_id: s.reason for s in self.skipped}


class ImportScopeError(ValueError):
    """A requested staged row is missing or outside the requested scope."""


def _payee_name_map(session: Session, payee_ids: Iterable[int | None]) -> dict[int, str]:
    ids = {pid for pid in payee_ids if pid is not None}
    if not ids:
        return {}
    return {p.id: p.name for p in session.exec(select(Payee)).all() if p.id in ids}


class ImportService:
    def __init__(self, session: Session) -> None:
        self.session = session
        # Set by commit() when an attempt failed, so the retry can clear the
        # failed transaction without rolling back a caller's pending work.
        self._needs_rollback = False

    # ---- staging ----

    def stage(
        self,
        parsed_rows: list[ParsedTransaction],
        account_id: int,
        batch_label: str,
    ) -> list[StagedTransaction]:
        """Classify and store parsed rows, returning them in input order.

        Classification considers the committed ledger *and* rows already staged
        for this account, including rows staged earlier in this same call, so a
        duplicate within one file - or a second batch staged before the first
        was committed - is caught. Such a match is recorded as FUZZY, never
        EXACT: an equal-valued pair of genuine purchases must reach the review
        queue, not vanish. Only the committed ledger can produce EXACT.
        """
        batch = batch_label or uuid.uuid4().hex
        existing = list(
            self.session.exec(select(Transaction).where(Transaction.account_id == account_id)).all()
        )
        payee_names = _payee_name_map(self.session, [t.payee_id for t in existing])

        # Uncommitted staged rows for this account are peers: an open batch, or
        # an earlier row of this very file. Rejected rows are not - the user has
        # already ruled on those.
        peers: list[StagedTransaction] = [
            s
            for s in self.session.exec(
                select(StagedTransaction).where(StagedTransaction.account_id == account_id)
            ).all()
            if s.committed_transaction_id is None and s.review_state is not ReviewState.REJECTED
        ]

        staged: list[StagedTransaction] = []
        for row in parsed_rows:
            candidate = Candidate(
                account_id=account_id,
                date=row.date,
                amount_cents=row.amount_cents,
                payee=row.payee_raw,
                imported_id=row.imported_id,
            )
            status = classify(candidate, existing, payee_names=payee_names)
            if status is not DedupStatus.EXACT and classify(candidate, peers) is not DedupStatus.NEW:
                # Any conflict with an uncommitted row is a review question, and
                # the newest row is the one that asks it.
                status = DedupStatus.FUZZY
            new_row = StagedTransaction(
                import_batch=batch,
                account_id=account_id,
                date=row.date,
                amount_cents=row.amount_cents,
                payee_raw=row.payee_raw,
                notes=row.notes,
                source_row=row.source_row,
                imported_id=row.imported_id,
                dedup_status=status,
                review_state=ReviewState.PENDING,
            )
            staged.append(new_row)
            peers.append(new_row)
        self.session.add_all(staged)
        self.session.commit()
        for s in staged:
            self.session.refresh(s)
        return staged

    def list_staged(
        self,
        *,
        account_id: int | None = None,
        batch: str | None = None,
        states: Sequence[ReviewState] | None = None,
        uncommitted_only: bool = True,
    ) -> list[StagedTransaction]:
        """The review queue, oldest first, for one account and/or batch."""
        stmt = select(StagedTransaction)
        if account_id is not None:
            stmt = stmt.where(StagedTransaction.account_id == account_id)
        if batch is not None:
            stmt = stmt.where(StagedTransaction.import_batch == batch)
        if states is not None:
            stmt = stmt.where(StagedTransaction.review_state.in_(list(states)))
        if uncommitted_only:
            stmt = stmt.where(StagedTransaction.committed_transaction_id.is_(None))
        rows = list(self.session.exec(stmt).all())
        return sorted(rows, key=lambda s: (s.id is None, s.id))

    # ---- review ----

    def set_review(self, staged_ids: int | list[int], state: ReviewState) -> list[StagedTransaction]:
        """Set the review state of one or more staged rows.

        REJECTED is terminal: accepting a rejected row is refused rather than
        quietly reversing a decision the user already made. Re-import the file
        to raise the row again.
        """
        ids = [staged_ids] if isinstance(staged_ids, int) else list(staged_ids)
        if not ids:
            return []
        rows = self.session.exec(select(StagedTransaction).where(StagedTransaction.id.in_(ids))).all()
        by_id = {r.id: r for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"no staged transaction {missing}")
        if state is not ReviewState.REJECTED:
            rejected = [r.id for r in rows if r.review_state is ReviewState.REJECTED]
            if rejected:
                raise ValueError(
                    f"staged transaction(s) {rejected} were rejected; re-stage the file to import them"
                )
        for row in rows:
            row.review_state = state
            self.session.add(row)
        self.session.commit()
        for row in rows:
            self.session.refresh(row)
        return sorted(rows, key=lambda s: (s.id is None, s.id))

    def accept(self, staged_ids: int | list[int]) -> list[StagedTransaction]:
        """Confirm staged rows: this is the explicit NEW/FUZZY acceptance step."""
        return self.set_review(staged_ids, ReviewState.ACCEPTED)

    def reject(self, staged_ids: int | list[int]) -> list[StagedTransaction]:
        return self.set_review(staged_ids, ReviewState.REJECTED)

    # ---- commit ----

    def commit(
        self,
        staged_ids: list[int],
        *,
        actor: str,
        account_id: int | None = None,
        batch: str | None = None,
    ) -> CommitResult:
        """Write the accepted, in-scope rows to the ledger in one transaction.

        ``account_id``/``batch`` scope the commit; a caller that means to commit
        one account's batch must not be able to sweep in another's rows.

        Two writers racing for the same staged row do not both win: the row is
        claimed with a compare-and-set update, and the unique index on
        ``transaction(account_id, imported_id)`` backs that up. The loser either
        sees ``already-committed`` on a retry or gets its unique-index conflict
        reconciled into the same answer - never a duplicate transaction.
        """

        def _op() -> CommitResult:
            # Only a rollback this method caused itself is safe here, so a
            # previous failed attempt is tracked rather than assumed.
            if self._needs_rollback:
                self.session.rollback()
                self._needs_rollback = False
            try:
                return self._commit_once(
                    staged_ids, actor=actor, account_id=account_id, batch=batch
                )
            except (OperationalError, IntegrityError):
                self._needs_rollback = True
                raise

        self._needs_rollback = False
        try:
            return transaction_with_retry(_op)
        except IntegrityError as exc:
            # Another writer won between our re-check and our write. Report the
            # state it left behind instead of surfacing a raw constraint error.
            self.session.rollback()
            self._needs_rollback = False
            return self._reconcile_conflict(staged_ids, exc)

    def _commit_once(
        self,
        staged_ids: list[int],
        *,
        actor: str,
        account_id: int | None,
        batch: str | None,
    ) -> CommitResult:
        if not staged_ids:
            return CommitResult()
        rows = self.session.exec(
            select(StagedTransaction).where(StagedTransaction.id.in_(list(staged_ids)))
        ).all()
        by_id = {r.id: r for r in rows}
        for sid in staged_ids:
            if sid not in by_id:
                raise ImportScopeError(f"no staged transaction {sid}")
        # A repeated id is a repeated click, not a request to write twice.
        seen: set[int] = set()
        ordered: list[StagedTransaction] = []
        for sid in staged_ids:
            if sid in seen:
                continue
            seen.add(sid)
            row = by_id[sid]
            if account_id is not None and row.account_id != account_id:
                raise ImportScopeError(
                    f"staged row {sid} belongs to account {row.account_id}, not {account_id}"
                )
            if batch is not None and row.import_batch != batch:
                raise ImportScopeError(
                    f"staged row {sid} belongs to batch {row.import_batch!r}, not {batch!r}"
                )
            ordered.append(row)

        accounts = {row.account_id for row in ordered}
        live: list[Transaction] = []
        for account in accounts:
            live.extend(
                self.session.exec(select(Transaction).where(Transaction.account_id == account)).all()
            )
        payee_names = _payee_name_map(self.session, [t.payee_id for t in live])

        created: list[Transaction] = []
        skipped: list[SkippedRow] = []
        for staged in ordered:
            if staged.committed_transaction_id is not None:
                skipped.append(SkippedRow(staged.id, SKIP_ALREADY_COMMITTED))
                continue
            if staged.review_state is ReviewState.REJECTED:
                skipped.append(SkippedRow(staged.id, SKIP_REJECTED))
                continue
            if staged.review_state is not ReviewState.ACCEPTED:
                skipped.append(SkippedRow(staged.id, SKIP_NEEDS_ACCEPTANCE))
                continue
            # Re-check against the live ledger: a row that became an exact
            # duplicate after staging (another batch, another window) is
            # skipped, not written twice.
            live_status = classify(
                Candidate(
                    account_id=staged.account_id,
                    date=staged.date,
                    amount_cents=staged.amount_cents,
                    payee=staged.payee_raw,
                    imported_id=staged.imported_id,
                ),
                live,
                payee_names=payee_names,
            )
            if live_status is DedupStatus.EXACT:
                skipped.append(SkippedRow(staged.id, SKIP_DUPLICATE_EXACT))
                continue

            payee = resolve_or_create_payee(self.session, staged.payee_raw)
            tx = Transaction(
                account_id=staged.account_id,
                date=staged.date,
                amount_cents=staged.amount_cents,
                payee_id=payee.id if payee else None,
                category_id=staged.suggested_category_id,
                notes=staged.notes,
                imported_id=staged.imported_id,
                created_by=actor,
            )
            self.session.add(tx)
            self.session.flush()
            now = datetime.now(UTC)
            claimed = self.session.execute(
                _CLAIM,
                {"tx_id": tx.id, "at": now, "actor": actor, "sid": staged.id},
            )
            if claimed.rowcount == 0:
                # Another session claimed this row first; our insert loses.
                self.session.delete(tx)
                skipped.append(SkippedRow(staged.id, SKIP_ALREADY_COMMITTED))
                continue
            staged.committed_transaction_id = tx.id
            staged.committed_at = now
            staged.committed_by = actor
            self.session.add(staged)
            audit.record(
                self.session,
                entity_type="transaction",
                entity_id=tx.id,
                action="create",
                actor=actor,
                after=tx,
            )
            created.append(tx)
            live.append(tx)
            if payee is not None:
                payee_names[payee.id] = payee.name
        self.session.commit()
        return CommitResult(created=created, skipped=skipped)

    def _reconcile_conflict(
        self, staged_ids: list[int], exc: IntegrityError
    ) -> CommitResult:
        """Turn a lost race into a report instead of an error.

        Any requested row that the winner committed is reported as
        ``already-committed``. A row that is still uncommitted, or that has
        gone away entirely, is not papered over: the original error is
        re-raised, because reporting success there would hide a real failure.
        """
        # populate_existing so this reads the winner's committed state rather
        # than the copy the losing transaction loaded before it lost.
        rows = self.session.exec(
            select(StagedTransaction)
            .where(StagedTransaction.id.in_(list(staged_ids)))
            .execution_options(populate_existing=True)
        ).all()
        found = {r.id: r for r in rows}
        unresolved: list[int] = []
        skipped: list[SkippedRow] = []
        for sid in dict.fromkeys(staged_ids):
            row = found.get(sid)
            if row is not None and row.committed_transaction_id is not None:
                skipped.append(SkippedRow(sid, SKIP_ALREADY_COMMITTED))
            else:
                unresolved.append(sid)
        if unresolved:
            raise exc
        return CommitResult(created=[], skipped=skipped)
