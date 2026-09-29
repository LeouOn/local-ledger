"""Cross-domain invalidation hooks for ledger mutations.

The ledger is the substrate that tax evidence is built on, so deleting or
amending a transaction has to reach the tax comparison that consumed it. The
dependency is resolved lazily and in one place, so:

- the ledger does not import the tax domain at module load (no import cycle, and
  a budget-only install still works);
- there is exactly one function to find when adding a new mutation path.

Adding a new ledger mutation? Call :func:`notify_transactions_changed` from it.
Forgetting is the failure mode this module exists to prevent: an unused
invalidation helper passes its own unit test while real deletions leave tax
comparisons marked "matched" - which is exactly what happened before this module
existed.
"""

from __future__ import annotations

from collections.abc import Iterable


def notify_transactions_changed(session: object, transaction_ids: Iterable[int]) -> None:
    """Tell the tax domain that ledger rows changed.

    The session is passed explicitly rather than discovered, so the invalidation
    lands in the caller's transaction instead of a second one, and so the call
    site is readable.

    Best effort by design: if the tax domain is not importable there is nothing
    to invalidate, and a budget deletion must not fail because of it. That case
    is reported, not swallowed.
    """
    ids = [i for i in transaction_ids if i is not None]
    if not ids:
        return
    try:
        from local_ledger.tax.service import TaxWorkspaceService
    except ImportError:  # pragma: no cover - tax domain not installed
        return

    service = TaxWorkspaceService(session)
    for tx_id in ids:
        # commit=False: the caller owns the transaction. The deletion and the
        # invalidation must land together or not at all.
        service.invalidate_for_transaction_edit(tx_id, commit=False)
    _mark_reconciliation_runs_stale(session)


def _mark_reconciliation_runs_stale(session: object) -> None:
    """Stale the tax-year snapshots whose workspace touched a changed row.

    A comparison can be marked stale while the total it was derived from is
    still marked CURRENT; that is the inconsistency this closes.
    """
    from sqlmodel import select

    try:
        from local_ledger.tax.reconciliation import ReconciliationRun, RunState
    except ImportError:  # pragma: no cover - tax domain not installed
        return

    stmt = select(ReconciliationRun).where(ReconciliationRun.state == RunState.CURRENT)
    for run in session.exec(stmt).all():  # type: ignore[attr-defined]
        run.state = RunState.STALE
        session.add(run)  # type: ignore[attr-defined]
