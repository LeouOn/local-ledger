"""Performance validation: large-ledger budgets for dashboard, payload, trends."""

from __future__ import annotations

import time

import pytest
from sqlmodel import Session

from local_ledger.analysis.payloads import PAYLOAD_TOKEN_CAP, build_evaluation_payload, to_prompt
from local_ledger.db import init_db
from local_ledger.ledger.trends import TrendsService
from tests.factories import make_account, make_category, make_transaction

DASHBOARD_BUDGET_S = 3.0
PAYLOAD_BUDGET_S = 2.0
TX_COUNT = 10_000
MONTHS = 36


@pytest.fixture()
def big_ledger(engine) -> Session:  # type: ignore[no-untyped-def]
    init_db(engine)
    session = Session(engine)
    acc = make_account(session)
    cats = [make_category(session, f"Cat{i}") for i in range(10)]
    per_month = TX_COUNT // MONTHS
    for m in range(1, MONTHS + 1):
        for i in range(per_month):
            cat = cats[i % len(cats)]
            make_transaction(
                session,
                acc.id,
                amount_cents=-(i + 1),
                date=f"2024-{(m - 1) % 12 + 1:02d}-{(i % 28) + 1:02d}",
                category_id=cat.id,
            )
    session.commit()
    return session


def test_dashboard_summary_under_budget(big_ledger: Session) -> None:
    trends = TrendsService(big_ledger)
    start = time.perf_counter()
    summary = trends.month_summary("2024-12")
    elapsed = time.perf_counter() - start
    assert elapsed < DASHBOARD_BUDGET_S, f"month_summary took {elapsed:.2f}s (budget {DASHBOARD_BUDGET_S}s)"
    assert summary.per_category  # produced real output


def test_payload_build_under_token_and_time_budget(big_ledger: Session) -> None:
    trends = TrendsService(big_ledger)
    summary = trends.month_summary("2024-12")
    start = time.perf_counter()
    payload = build_evaluation_payload(month="2024-12", summary=summary, mom_income=[], mom_spent=[])
    prompt = to_prompt(payload)
    elapsed = time.perf_counter() - start

    from local_ledger.analysis.payloads import _encoder

    tokens = _encoder()(prompt)
    assert elapsed < PAYLOAD_BUDGET_S, f"payload build took {elapsed:.2f}s"
    assert tokens <= PAYLOAD_TOKEN_CAP, f"payload was {tokens} tokens (cap {PAYLOAD_TOKEN_CAP})"
