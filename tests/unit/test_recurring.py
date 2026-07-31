"""Recurring detection tests: grouping, tolerance, intervals, no LLM."""

from __future__ import annotations

import inspect

from local_ledger.automation.recurring import detect_recurrences
from local_ledger.models import Transaction


def _tx(date_str: str, amount: int, notes: str, tx_id: int | None = None) -> Transaction:
    return Transaction(
        id=tx_id, account_id=1, date=date_str, amount_cents=amount, notes=notes
    )


def test_detects_monthly_recurring() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix", 1),
        _tx("2026-02-05", -1000, "Netflix", 2),
        _tx("2026-03-05", -1000, "Netflix", 3),
    ]
    groups = detect_recurrences(txs)
    assert len(groups) == 1
    g = groups[0]
    assert g.payee == "netflix"
    assert g.amount_cents == 1000
    assert g.interval_days == 30  # ~monthly
    assert g.transaction_ids == [1, 2, 3]
    assert g.confidence > 0.5


def test_does_not_detect_one_off() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix"),
        _tx("2026-03-05", -1500, "Netflix"),
    ]
    groups = detect_recurrences(txs)
    assert groups == []


def test_tolerance_allows_amount_variation() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Electric"),
        _tx("2026-02-05", -1080, "Electric"),
        _tx("2026-03-05", -1000, "Electric"),
    ]
    groups = detect_recurrences(txs)
    assert len(groups) == 1


def test_requires_minimum_three_occurrences() -> None:
    txs = [
        _tx("2026-01-05", -1000, "Netflix"),
        _tx("2026-02-05", -1000, "Netflix"),
    ]
    assert detect_recurrences(txs) == []


def test_regular_interval_high_confidence() -> None:
    """Transactions exactly 30 days apart get high confidence."""
    txs = [
        _tx("2026-01-05", -2000, "Gym"),
        _tx("2026-02-04", -2000, "Gym"),
        _tx("2026-03-06", -2000, "Gym"),
    ]
    groups = detect_recurrences(txs)
    assert len(groups) == 1
    assert groups[0].confidence > 0.8


def test_recurring_module_has_no_llm_imports() -> None:
    from local_ledger.automation import recurring

    src = inspect.getsource(recurring)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
