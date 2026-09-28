"""Dedup engine: classify a candidate against existing transactions.

Pure logic - no session, no writes, never auto-merges. Callers (the import
service) decide what to do with each classification; FUZZY candidates go to a
review queue, never straight into the ledger.

A ledger transaction stores its payee as ``payee_id``, so comparing payees needs
the ``Payee`` rows, which this module cannot fetch (it holds no session). The
caller therefore passes ``payee_names``, a mapping of ``payee_id`` to payee
name. Staged rows are compared directly: they carry the raw ``payee_raw``
string, so an uncommitted row needs no session either.

``Transaction.notes`` is deliberately *not* a payee. Notes are free text, and
treating them as identity made real payees invisible while inventing matches
that had nothing to do with the payee. A row with no payee is compared on its
source id alone: it can be EXACT, never FUZZY.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from local_ledger.models import DedupStatus, StagedTransaction, Transaction

_PUNCT = re.compile(r"[^a-z0-9]+")

# Either a committed transaction or an uncommitted staged row: both carry
# account, date, amount, source id, and a payee (FK for the first, raw text for
# the second).
Existing = Transaction | StagedTransaction


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


def _payee_of(row: Existing, payee_names: Mapping[int, str] | None = None) -> str:
    """Resolve the payee name for ``row``, or "" when it has no payee."""
    raw = getattr(row, "payee_raw", None)
    if raw:
        return raw
    if payee_names is not None and row.payee_id is not None:
        return payee_names.get(row.payee_id) or ""
    return ""


def _within_days(a: str, b: str, window: int) -> bool:
    da = datetime.fromisoformat(a).date()
    db = datetime.fromisoformat(b).date()
    return abs((da - db).days) <= window


def classify(
    candidate: Candidate,
    existing: Sequence[Existing],
    window_days: int = 7,
    payee_names: Mapping[int, str] | None = None,
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
        if cand_norm and normalize_payee(_payee_of(tx, payee_names)) == cand_norm:
            return DedupStatus.FUZZY

    # 3) NEW: no match.
    return DedupStatus.NEW


def classify_many(
    candidates: list[Candidate],
    existing: Sequence[Existing],
    window_days: int = 7,
    payee_names: Mapping[int, str] | None = None,
) -> list[DedupStatus]:
    return [classify(c, existing, window_days=window_days, payee_names=payee_names) for c in candidates]
