"""Analysis payload builder tests: aggregates only, ≤4096 tokens, rejects raw transactions."""

from __future__ import annotations

import pytest
import tiktoken
from sqlmodel import Session

from local_ledger.analysis.payloads import (
    PAYLOAD_TOKEN_CAP,
    PrivacyViolation,
    build_evaluation_payload,
    to_prompt,
)
from local_ledger.db import init_db
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import Transaction
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


def _seed_ledger(session: Session, n_months: int = 1, tx_per_month: int = 5):  # type: ignore[no-untyped-def]
    acc = make_account(session)
    food = make_category(session, "Food")
    income = make_category(session, "Salary", is_income=True)
    for m in range(1, n_months + 1):
        make_transaction(
            session,
            acc.id,
            amount_cents=100000,
            date=f"2026-{m:02d}-01",
            category_id=income.id,
        )
        for i in range(tx_per_month):
            make_transaction(
                session,
                acc.id,
                amount_cents=-(1000 + i * 100),
                date=f"2026-{m:02d}-{i + 2:02d}",
                category_id=food.id,
            )
    session.commit()
    return acc, food, income


def test_payload_contains_only_aggregates(session: Session) -> None:
    _seed_ledger(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")

    payload = build_evaluation_payload(
        month="2026-01",
        summary=summary,
        mom_income=[("2025-12", 100000)],
        mom_spent=[("2025-12", 6000)],
    )
    dumped = payload.model_dump_json()
    # no raw-transaction fields leak
    for forbidden in ("payee", "account", "notes", "imported_id", "transferred_id"):
        assert forbidden not in dumped.lower(), f"payload leaked {forbidden!r}"


def test_to_prompt_under_token_cap(session: Session) -> None:
    _seed_ledger(session, n_months=1, tx_per_month=5)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    payload = build_evaluation_payload(
        month="2026-01", summary=summary, mom_income=[], mom_spent=[]
    )
    prompt = to_prompt(payload)
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(prompt)) <= PAYLOAD_TOKEN_CAP


def test_large_ledger_payload_stays_under_cap(session: Session) -> None:
    """AC14: a 10K-tx / 36-month ledger must still produce a <=cap prompt.

    Aggregates collapse thousands of transactions into per-category totals, so
    the prompt is bounded by category count, not transaction count.
    """
    acc = make_account(session)
    cats = [make_category(session, f"Cat{i}") for i in range(20)]
    # many transactions, few categories -> aggregate is small
    for m in range(1, 13):
        for i in range(800):
            cat = cats[i % len(cats)]
            make_transaction(
                session, acc.id, amount_cents=-(i + 1), date=f"2026-{m:02d}-{(i % 28) + 1:02d}", category_id=cat.id
            )
    session.commit()

    trends = TrendsService(session)
    summary = trends.month_summary("2026-12")
    payload = build_evaluation_payload(
        month="2026-12", summary=summary, mom_income=[], mom_spent=[]
    )
    prompt = to_prompt(payload)
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = len(enc.encode(prompt))
    assert tokens <= PAYLOAD_TOKEN_CAP, f"prompt was {tokens} tokens (cap {PAYLOAD_TOKEN_CAP})"


def test_payload_rejects_raw_transaction_input() -> None:
    """A raw Transaction must never be accepted as payload input."""
    tx = Transaction(account_id=1, date="2026-01-01", amount_cents=-500)
    with pytest.raises(PrivacyViolation):
        build_evaluation_payload(month="2026-01", summary=tx, mom_income=[], mom_spent=[])  # type: ignore[arg-type]


def test_payload_truncates_categories_to_respect_cap(session: Session) -> None:
    """If category count alone would exceed the cap, the builder truncates and signals."""
    acc = make_account(session)
    # 500 categories each appearing this month
    for i in range(500):
        cat = make_category(session, f"VeryLongCategoryName{i}")
        make_transaction(session, acc.id, amount_cents=-100, date="2026-01-05", category_id=cat.id)
    session.commit()

    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    payload = build_evaluation_payload(
        month="2026-01", summary=summary, mom_income=[], mom_spent=[]
    )
    prompt = to_prompt(payload)
    enc = tiktoken.get_encoding("cl100k_base")
    assert len(enc.encode(prompt)) <= PAYLOAD_TOKEN_CAP
    # truncation flag set
    assert payload.truncated is True


def test_payload_uses_real_category_names_not_placeholders(session: Session) -> None:
    """Oracle #8: the cloud payload must carry real category names, not category_<id>."""
    _seed_ledger(session)
    trends = TrendsService(session)
    summary = trends.month_summary("2026-01")
    payload = build_evaluation_payload(
        month="2026-01", summary=summary, mom_income=[], mom_spent=[]
    )
    names = [c.name for c in payload.categories]
    assert "Food" in names
    assert not any(n.startswith("category_") for n in names)
