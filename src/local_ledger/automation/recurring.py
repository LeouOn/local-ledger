"""Recurring transaction detection: group by payee + similar amount + regular interval. No LLM."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from local_ledger.ledger.dedup import normalize_payee
from local_ledger.models import Transaction

MIN_OCCURRENCES = 3


@dataclass
class RecurringGroup:
    payee: str
    amount_cents: int
    interval_days: int
    transaction_ids: list[int]
    confidence: float


def detect_recurrences(
    transactions: list[Transaction],
    tolerance_pct: float = 10.0,
) -> list[RecurringGroup]:
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for tx in transactions:
        payee = normalize_payee(tx.notes)
        if payee:
            groups[payee].append(tx)

    results: list[RecurringGroup] = []
    for payee, txs in groups.items():
        if len(txs) < MIN_OCCURRENCES:
            continue
        txs_sorted = sorted(txs, key=lambda t: t.date)
        amounts = [abs(t.amount_cents) for t in txs_sorted]
        base = amounts[0]
        if base == 0:
            continue
        in_tolerance = all(abs(a - base) / base <= tolerance_pct / 100 for a in amounts)
        if not in_tolerance:
            continue

        dates = [datetime.fromisoformat(t.date) for t in txs_sorted]
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        if avg_interval <= 0:
            continue

        interval_days = round(avg_interval)
        variance = (
            max((abs(iv - avg_interval) / avg_interval) for iv in intervals) if intervals else 0
        )
        confidence = round(max(0.0, 1.0 - variance), 2)
        if variance > 0.4:
            continue

        results.append(
            RecurringGroup(
                payee=payee,
                amount_cents=base,
                interval_days=interval_days,
                transaction_ids=[t.id for t in txs_sorted if t.id is not None],
                confidence=confidence,
            )
        )
    return results
