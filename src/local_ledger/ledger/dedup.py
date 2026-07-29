"""Dedup engine: classify a candidate against existing transactions.

Pure logic - no session, no writes, never auto-merges. Callers (the import
service, todo 13) decide what to do with each classification; FUZZY candidates
go to a review queue, never straight into the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from local_ledger.models import DedupStatus, Transaction

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize_payee(payee: str | None) -> str:
    if not payee:
        return ""
    return _PUNCT.sub("", payee.lower().strip())


@dataclass(frozen=True)
class Candidate:
    account_id: int
    date: str  # YYYY-MM-DD
    amount_cents: int
    payee: str | None = None
    imported_id: str | None = None


def _payee_of(tx: Transaction) -> str:
    """Resolve the raw payee string from a stored Transaction.

    Transactions store a payee_id FK; in tests and staging we also carry the raw
    payee text in ``notes``. The engine prefers notes when present.
    """
    return tx.notes or ""


def _within_days(a: str, b: str, window: int) -> bool:
    da = datetime.fromisoformat(a).date()
    db = datetime.fromisoformat(b).date()
    return abs((da - db).days) <= window


def classify(
    candidate: Candidate,
    existing: list[Transaction],
    window_days: int = 7,
) -> DedupStatus:
    # 1) EXACT: same imported_id within the same account.
    if candidate.imported_id is not None:
        for tx in existing:
            if (
                tx.account_id == candidate.account_id
                and tx.imported_id == candidate.imported_id
            ):
                return DedupStatus.EXACT

    # 2) FUZZY: same account, exact |amount|, date within window, same normalized payee.
    cand_norm = normalize_payee(candidate.payee)
    for tx in existing:
        if tx.account_id != candidate.account_id:
            continue
        if abs(tx.amount_cents) != abs(candidate.amount_cents):
            continue
        if tx.amount_cents != candidate.amount_cents:  # sign-aware
            continue
        if not _within_days(tx.date, candidate.date, window_days):
            continue
        if cand_norm and normalize_payee(_payee_of(tx)) == cand_norm:
            return DedupStatus.FUZZY

    # 3) NEW: no match.
    return DedupStatus.NEW


def classify_many(
    candidates: list[Candidate],
    existing: list[Transaction],
    window_days: int = 7,
) -> list[DedupStatus]:
    return [classify(c, existing, window_days=window_days) for c in candidates]
