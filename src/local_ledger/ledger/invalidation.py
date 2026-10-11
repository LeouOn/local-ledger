"""Invalidate tax snapshots in the ledger mutation's own transaction."""

from __future__ import annotations

from collections.abc import Iterable

from sqlmodel import Session, select


def notify_transactions_changed(
    session: Session,
    transaction_ids: Iterable[int],
    *,
    prior_account_years: Iterable[tuple[int, int]] = (),
) -> None:
    """Cover creates, imports, edits and deletions, including previously empty runs.

    Editors that move an account/date must pass the old account/year as well.
    Read deletion coordinates before autoflush can remove the source rows.
    Invalidation never commits independently of the ledger change.
    """
    ids = [i for i in transaction_ids if i is not None]
    if not ids:
        return
    from local_ledger.models import Transaction
    from local_ledger.tax.classification import ClassificationSubject, DecisionHead
    from local_ledger.tax.models import RentalExpenseRecord, TaxWorkspace
    from local_ledger.tax.scope import ReviewScope
    from local_ledger.tax.service import TaxWorkspaceService

    with session.no_autoflush:
        rows = session.exec(select(Transaction).where(Transaction.id.in_(ids))).all()
        coordinates = set(prior_account_years)
        coordinates.update((r.account_id, int(r.date[:4])) for r in rows)
        workspaces = {w.id: w for w in session.exec(select(TaxWorkspace)).all()}
        affected = {
            scope.workspace_id
            for scope in session.exec(select(ReviewScope)).all()
            if scope.workspace_id in workspaces and any(
                scope.covers_account(account) and workspaces[scope.workspace_id].tax_year == year
                for account, year in coordinates
            )
        }
        affected.update(r.workspace_id for r in session.exec(
            select(RentalExpenseRecord).where(RentalExpenseRecord.source_transaction_id.in_(ids))
        ).all())
        affected.update(h.workspace_id for h in session.exec(select(DecisionHead).where(
            DecisionHead.subject_type == ClassificationSubject.TRANSACTION,
            DecisionHead.subject_id.in_([str(i) for i in ids]),
        )).all())
    service = TaxWorkspaceService(session)
    for tx_id in ids:
        service.invalidate_for_transaction_edit(tx_id, commit=False)
    for workspace_id in affected:
        service._invalidate_downstream(workspace_id)
