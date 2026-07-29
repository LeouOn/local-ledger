"""Dedup engine tests: EXACT/FUZZY/NEW classification, never auto-merge."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from local_ledger.ledger.dedup import Candidate, classify, classify_many, normalize_payee
from local_ledger.models import DedupStatus, Transaction


def _existing(account_id: int, date: str, amount: int, payee: str | None = None, imported_id: str | None = None) -> Transaction:
    return Transaction(
        account_id=account_id, date=date, amount_cents=amount, imported_id=imported_id, payee_id=None
    )  # payee string carried via a side attribute for tests


def _tx_with_payee(account_id: int, date: str, amount: int, payee: str, imported_id: str | None = None) -> Transaction:
    tx = _existing(account_id, date, amount, imported_id=imported_id)
    tx.notes = payee  # carry the raw payee string in notes for the pure-logic tests
    return tx


# --- normalize_payee ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Coffee Shop", "coffeeshop"),
        ("  Coffee-Shop!! ", "coffeeshop"),
        ("STAR-bucks", "starbucks"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_normalize_payee(raw: str, expected: str) -> None:
    assert normalize_payee(raw) == expected


# --- EXACT ---


def test_exact_match_on_imported_id_same_account() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A", imported_id="FIT-1")
    existing = [_tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")]
    assert classify(cand, existing) == DedupStatus.EXACT


def test_exact_requires_imported_id() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A", imported_id=None)
    existing = [_tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")]
    # candidate has no imported_id; cannot be exact even if existing has one -> fuzzy if payee matches
    assert classify(cand, existing) == DedupStatus.FUZZY


def test_exact_skips_without_queue_in_classify_many() -> None:
    cands = [Candidate(1, "2026-01-10", -1500, "A", imported_id="FIT-1")]
    existing = [_tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")]
    statuses = classify_many(cands, existing)
    assert statuses == [DedupStatus.EXACT]


# --- NEW ---


def test_no_match_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-02-01", -9999, "Z")]
    assert classify(cand, existing) == DedupStatus.NEW


# --- FUZZY (within ±7 days, same |amount|, same normalized payee, same account) ---


def test_fuzzy_match_within_window() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="Coffee Shop")
    existing = [_tx_with_payee(1, "2026-01-13", -1500, "coffee shop")]
    assert classify(cand, existing) == DedupStatus.FUZZY


def test_fuzzy_amount_off_by_one_cent_is_new() -> None:
    """No false positives: a 1-cent difference must NOT fuzzy-match."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-01-10", -1501, "A")]
    assert classify(cand, existing) == DedupStatus.NEW


def test_fuzzy_outside_window_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-01-19", -1500, "A")]  # 9 days out
    assert classify(cand, existing) == DedupStatus.NEW


def test_fuzzy_window_is_configurable() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-01-19", -1500, "A")]  # 9 days
    assert classify(cand, existing, window_days=7) == DedupStatus.NEW
    assert classify(cand, existing, window_days=12) == DedupStatus.FUZZY


def test_fuzzy_different_payee_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-01-11", -1500, "B")]
    assert classify(cand, existing) == DedupStatus.NEW


def test_fuzzy_different_account_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(2, "2026-01-10", -1500, "A")]
    assert classify(cand, existing) == DedupStatus.NEW


def test_fuzzy_sign_aware_amount() -> None:
    """|amount| must match; a refund (+1500) is not the same txn as a charge (-1500)."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    existing = [_tx_with_payee(1, "2026-01-11", 1500, "A")]
    assert classify(cand, existing) == DedupStatus.NEW


# --- engine never auto-merges ---


def test_engine_never_writes_or_merges() -> None:
    """classify* return statuses only; the engine holds no session and writes nothing."""
    import inspect

    from local_ledger.ledger import dedup

    src = inspect.getsource(dedup)
    for token in ("session.add", "session.commit", "session.delete", "session.merge"):
        assert token not in src, f"dedup engine must not {token}"


# --- 100-pair recall fixture (>=95% routed FUZZY, 0 auto-merge) ---


def test_100_pair_fixture_recall() -> None:
    base = datetime(2026, 1, 1)
    existing = []
    candidates = []
    for i in range(100):
        delta = timedelta(days=i % 7)  # within 7-day window
        edate = (base + delta).strftime("%Y-%m-%d")
        existing.append(_tx_with_payee(1, edate, -2000 - i, "Recurring Payee"))
        cdate = (base + timedelta(days=(i % 7) + 1)).strftime("%Y-%m-%d")
        candidates.append(Candidate(1, cdate, -2000 - i, "recurring payee"))
    statuses = classify_many(candidates, existing)
    fuzzy = sum(1 for s in statuses if s == DedupStatus.FUZZY)
    assert fuzzy >= 95
    # EXACT count is 0 (no imported_ids) and NEW is the remainder; nothing was merged.
    assert all(s in (DedupStatus.FUZZY, DedupStatus.NEW) for s in statuses)
