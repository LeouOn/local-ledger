"""Staging review UI: the one place where staged rows are reviewed and committed.

Both the Import page (per batch) and the Transactions page (account-wide) render
this block, so the two entry points cannot drift apart. The flow is deliberately
two-step - select rows, accept or reject them, then commit what is accepted -
because a commit button that also implies confirmation is how a pending fuzzy
row ends up in the ledger.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from local_ledger.ingest.importer import CommitResult, ImportService
from local_ledger.models import ReviewState, StagedTransaction


def _label(row: StagedTransaction) -> str:
    return (
        f"#{row.id} | {row.date} | {row.amount_cents / 100:+.2f} | "
        f"{row.payee_raw or '(no payee)'} | {row.dedup_status}"
    )


def _table(rows: list[StagedTransaction]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": r.id,
                "row": r.source_row,
                "date": r.date,
                "amount": f"{r.amount_cents / 100:+.2f}",
                "payee": r.payee_raw or "",
                "notes": r.notes or "",
                "dedup": str(r.dedup_status),
                "review": str(r.review_state),
                "source_id": r.imported_id or "",
            }
            for r in rows
        ]
    )


def render_commit_result(summary: dict | None) -> None:
    """Report what was written and why anything else was not.

    The summary is a plain dict because the page reruns after a commit, and
    Streamlit drops everything written before a rerun.
    """
    if not summary:
        return
    created = int(summary.get("created", 0))
    skipped: list[tuple[int, str]] = list(summary.get("skipped", []))
    if created:
        st.success(f"Committed {created} transaction(s).")
    if skipped:
        counts: dict[str, int] = {}
        for _sid, reason in skipped:
            counts[reason] = counts.get(reason, 0) + 1
        detail = ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items()))
        st.warning(f"Skipped {len(skipped)} staged row(s): {detail}.")
        st.dataframe(
            pd.DataFrame(
                [{"id": sid, "reason": reason} for sid, reason in skipped],
                columns=["id", "reason"],
            ),
            use_container_width=False,
        )


def summarize(result: CommitResult) -> dict:
    return {
        "created": len(result.created),
        "skipped": [(s.staged_id, s.reason) for s in result.skipped],
    }


def render_staged_review(
    importer: ImportService,
    *,
    account_id: int | None,
    batch: str | None,
    actor: str,
    key_prefix: str,
    allow_commit: bool = True,
) -> None:
    """Render the review queue for one account and/or batch."""
    result_key = f"{key_prefix}_result"
    render_commit_result(st.session_state.pop(result_key, None))

    rows = importer.list_staged(
        account_id=account_id,
        batch=batch,
        states=[ReviewState.PENDING, ReviewState.ACCEPTED],
    )
    if not rows:
        st.caption("No staged rows awaiting review.")
        return

    st.dataframe(_table(rows), use_container_width=False)
    selected = st.multiselect(
        "Rows to review",
        [_label(r) for r in rows],
        key=f"{key_prefix}_rows",
        help="Select rows, then accept or reject them. Only accepted rows are committed.",
    )
    chosen = set(selected)
    ids = [r.id for r in rows if _label(r) in chosen and r.id is not None]
    accepted = [r.id for r in rows if r.review_state is ReviewState.ACCEPTED and r.id is not None]

    review_col, commit_col = st.columns(2)
    with review_col:
        b1, b2 = st.columns(2)
        if b1.button("Accept selected", key=f"{key_prefix}_accept", disabled=not ids):
            importer.accept(ids)
            st.rerun()
        if b2.button("Reject selected", key=f"{key_prefix}_reject", disabled=not ids):
            importer.reject(ids)
            st.rerun()
    with commit_col:
        if not allow_commit:
            st.caption("Commit from the Import page so the batch stays in scope.")
        elif st.button(
            f"Commit {len(accepted)} accepted row(s)",
            key=f"{key_prefix}_commit",
            disabled=not accepted,
        ):
            result = importer.commit(accepted, actor=actor, account_id=account_id, batch=batch)
            st.session_state[result_key] = summarize(result)
            st.rerun()
