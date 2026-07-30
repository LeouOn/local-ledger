"""Multi-month payload builder tests: aggregates only, token-capped."""

from __future__ import annotations

import json

import tiktoken

from local_ledger.analysis.multimonth import (
    MultiMonthPayload,
    build_multimonth_payload,
    to_prompt,
)
from local_ledger.ledger.trends import CategoryMonth, MonthSummary

TOKEN_CAP = 4096


def _make_summary(month: str, spent: int, income: int = 100000) -> MonthSummary:
    return MonthSummary(
        month=month,
        income_cents=income,
        spent_cents=spent,
        budgeted_cents=30000,
        per_category=[
            CategoryMonth(
                category_id=1,
                name="Food",
                budgeted=30000,
                spent=spent,
                balance=30000 - spent,
                carryover=0,
            ),
        ],
    )


def test_payload_aggregates_multiple_months() -> None:
    summaries = [
        _make_summary("2026-01", 25000),
        _make_summary("2026-02", 35000),
        _make_summary("2026-03", 40000),
    ]
    payload = build_multimonth_payload(
        months=["2026-01", "2026-02", "2026-03"],
        summaries=summaries,
    )
    assert isinstance(payload, MultiMonthPayload)
    assert payload.total_income_cents == 300000
    assert payload.total_spent_cents == 100000
    assert payload.months == ["2026-01", "2026-02", "2026-03"]
    cat = payload.categories[1]
    assert cat.months == [25000, 35000, 40000]


def test_trend_direction_computed() -> None:
    summaries = [
        _make_summary("2026-01", 10000),
        _make_summary("2026-02", 15000),
    ]
    payload = build_multimonth_payload(months=["2026-01", "2026-02"], summaries=summaries)
    cat = payload.categories[1]
    assert cat.trend_direction == "up"


def test_trend_direction_flat() -> None:
    summaries = [
        _make_summary("2026-01", 10000),
        _make_summary("2026-02", 10500),
    ]
    payload = build_multimonth_payload(months=["2026-01", "2026-02"], summaries=summaries)
    cat = payload.categories[1]
    assert cat.trend_direction == "flat"


def test_trend_direction_down() -> None:
    summaries = [
        _make_summary("2026-01", 20000),
        _make_summary("2026-02", 10000),
    ]
    payload = build_multimonth_payload(months=["2026-01", "2026-02"], summaries=summaries)
    cat = payload.categories[1]
    assert cat.trend_direction == "down"


def test_to_prompt_under_token_cap() -> None:
    summaries = [_make_summary(f"2026-{m:02d}", 20000 + m * 1000) for m in range(1, 13)]
    payload = build_multimonth_payload(
        months=[f"2026-{m:02d}" for m in range(1, 13)],
        summaries=summaries,
    )
    prompt = to_prompt(payload)
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(prompt)) <= TOKEN_CAP


def test_truncation_when_over_cap() -> None:
    """A large category count across many months triggers truncation."""
    cats = [
        CategoryMonth(
            category_id=i,
            name=f"Cat{i}",
            budgeted=100,
            spent=i * 100,
            balance=0,
            carryover=0,
        )
        for i in range(50)
    ]
    summaries = [
        MonthSummary(
            month=f"2026-{m:02d}",
            income_cents=100000,
            spent_cents=50000,
            budgeted_cents=5000,
            per_category=cats,
        )
        for m in range(1, 13)
    ]
    payload = build_multimonth_payload(
        months=[f"2026-{m:02d}" for m in range(1, 13)],
        summaries=summaries,
    )
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(to_prompt(payload))) <= TOKEN_CAP
    if payload.truncated:
        assert len(payload.months) < 12


def test_no_raw_transaction_fields_in_payload() -> None:
    summaries = [_make_summary("2026-01", 25000)]
    payload = build_multimonth_payload(months=["2026-01"], summaries=summaries)
    dumped = json.dumps(payload.model_dump())
    for forbidden in ("payee", "account", "notes", "imported_id"):
        assert forbidden not in dumped.lower()
