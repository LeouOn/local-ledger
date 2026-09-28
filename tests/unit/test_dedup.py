"""Dedup engine tests: EXACT/FUZZY/NEW classification, never auto-merge.

Payee identity comes from the ``payee_id`` foreign key (resolved by the caller)
or from a staged row's ``payee_raw``. ``Transaction.notes`` is free text and is
never used as a payee - the earlier engine did, which both hid real payees and
invented matches that had nothing to do with the payee.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from local_ledger.ledger.dedup import Candidate, classify, classify_many, normalize_payee
from local_ledger.models import DedupStatus, StagedTransaction, Transaction

# Stand-in payee ids; the tests never touch a database, they only hand the
# engine the id -> name resolution a session would provide.
COFFEE_ID = 41


def _existing(account_id: int, date: str, amount: int, payee_id=None, imported_id=None, **kw) -> Transaction:
    return Transaction(
        account_id=account_id,
        date=date,
        amount_cents=amount,
        imported_id=imported_id,
        payee_id=payee_id,
        **kw,
    )


def _tx_with_payee(
    account_id: int, date: str, amount: int, payee: str, imported_id: str | None = None
) -> tuple[Transaction, dict[int, str]]:
    """A transaction whose payee resolves through its FK, plus that resolution."""
    return _existing(account_id, date, amount, payee_id=COFFEE_ID, imported_id=imported_id), {
        COFFEE_ID: payee
    }


def _staged(account_id: int, date: str, amount: int, payee: str, imported_id=None) -> StagedTransaction:
    return StagedTransaction(
        import_batch="b",
        account_id=account_id,
        date=date,
        amount_cents=amount,
        payee_raw=payee,
        imported_id=imported_id,
    )


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
    tx, names = _tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.EXACT


def test_exact_requires_imported_id() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A", imported_id=None)
    tx, names = _tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")
    # candidate has no imported_id; cannot be exact even if existing has one -> fuzzy if payee matches
    assert classify(cand, [tx], payee_names=names) == DedupStatus.FUZZY


def test_exact_skips_without_queue_in_classify_many() -> None:
    cands = [Candidate(1, "2026-01-10", -1500, "A", imported_id="FIT-1")]
    tx, names = _tx_with_payee(1, "2026-01-10", -1500, "A", imported_id="FIT-1")
    assert classify_many(cands, [tx], payee_names=names) == [DedupStatus.EXACT]


def test_exact_is_account_scoped() -> None:
    """The same source id in another account is a different row, not a duplicate."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, imported_id="FIT-1")
    other, names = _tx_with_payee(2, "2026-01-10", -1500, "Z", imported_id="FIT-1")
    assert classify(cand, [other], payee_names=names) == DedupStatus.NEW


# --- NEW ---


def test_no_match_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-02-01", -9999, "Z")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


def test_notes_are_not_a_payee() -> None:
    """A payee-looking note must not create a match; identity is the FK."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="Coffee Shop")
    tx = _existing(1, "2026-01-10", -1500, notes="coffee shop")
    assert classify(cand, [tx], payee_names={COFFEE_ID: "Coffee Shop"}) == DedupStatus.NEW


def test_unresolvable_payee_id_does_not_match() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="Coffee Shop")
    tx = _existing(1, "2026-01-10", -1500, payee_id=999)
    assert classify(cand, [tx], payee_names={COFFEE_ID: "Coffee Shop"}) == DedupStatus.NEW


# --- FUZZY (within ±7 days, same |amount|, same normalized payee, same account) ---


def test_fuzzy_match_within_window() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="Coffee Shop")
    tx, names = _tx_with_payee(1, "2026-01-13", -1500, "coffee shop")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.FUZZY


def test_fuzzy_amount_off_by_one_cent_is_new() -> None:
    """No false positives: a 1-cent difference must NOT fuzzy-match."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-01-10", -1501, "A")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


def test_fuzzy_outside_window_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-01-19", -1500, "A")  # 9 days out
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


def test_fuzzy_window_is_configurable() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-01-19", -1500, "A")  # 9 days
    assert classify(cand, [tx], window_days=7, payee_names=names) == DedupStatus.NEW
    assert classify(cand, [tx], window_days=12, payee_names=names) == DedupStatus.FUZZY


def test_fuzzy_different_payee_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-01-11", -1500, "B")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


def test_fuzzy_different_account_is_new() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(2, "2026-01-10", -1500, "A")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


def test_fuzzy_sign_aware_amount() -> None:
    """|amount| must match; a refund (+1500) is not the same txn as a charge (-1500)."""
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="A")
    tx, names = _tx_with_payee(1, "2026-01-11", 1500, "A")
    assert classify(cand, [tx], payee_names=names) == DedupStatus.NEW


# --- staged rows are comparable without a session ---


def test_staged_row_matches_on_payee_raw() -> None:
    cand = Candidate(account_id=1, date="2026-01-10", amount_cents=-1500, payee="coffee-shop")
    assert classify(cand, [_staged(1, "2026-01-12", -1500, "Coffee Shop")]) == DedupStatus.FUZZY


def test_staged_row_matches_on_imported_id() -> None:
    cand = Candidate(account_id=1, date="2026-02-09", amount_cents=-1500, imported_id="FIT-2")
    assert classify(cand, [_staged(1, "2026-01-01", -9999, "Other", imported_id="FIT-2")]) == (
        DedupStatus.EXACT
    )


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
    names = {}
    candidates = []
    for i in range(100):
        delta = timedelta(days=i % 7)  # within 7-day window
        edate = (base + delta).strftime("%Y-%m-%d")
        existing.append(_existing(1, edate, -2000 - i, payee_id=COFFEE_ID))
        cdate = (base + timedelta(days=(i % 7) + 1)).strftime("%Y-%m-%d")
        candidates.append(Candidate(1, cdate, -2000 - i, "recurring payee"))
    names[COFFEE_ID] = "Recurring Payee"
    statuses = classify_many(candidates, existing, payee_names=names)
    fuzzy = sum(1 for s in statuses if s == DedupStatus.FUZZY)
    assert fuzzy >= 95
    # EXACT count is 0 (no imported_ids) and NEW is the remainder; nothing was merged.
    assert all(s in (DedupStatus.FUZZY, DedupStatus.NEW) for s in statuses)
