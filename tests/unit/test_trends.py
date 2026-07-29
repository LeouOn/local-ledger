"""Trends engine tests: month summary, on-read carryover, back-dated propagation."""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import CategoryBudget
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture()
def trends(session: Session, engine) -> TrendsService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return TrendsService(session)


def _budget(session, category_id, month, allocated, carryover=False):  # type: ignore[no-untyped-def]
    session.add(
        CategoryBudget(
            category_id=category_id, month=month, allocated_cents=allocated, carryover_enabled=carryover
        )
    )
    session.commit()


def test_empty_month_is_all_zeros_no_exception(trends: TrendsService) -> None:
    s = trends.month_summary("2026-03")
    assert s.income_cents == 0
    assert s.spent_cents == 0
    assert s.budgeted_cents == 0
    assert s.per_category == []


def test_month_summary_income_and_spent(trends: TrendsService, session: Session) -> None:
    acc = make_account(session)
    income_cat = make_category(session, "Salary", is_income=True)
    food_cat = make_category(session, "Food")
    make_transaction(session, acc.id, amount_cents=200000, date="2026-01-05", category_id=income_cat.id)
    make_transaction(session, acc.id, amount_cents=-5000, date="2026-01-06", category_id=food_cat.id)
    make_transaction(session, acc.id, amount_cents=-1500, date="2026-01-07", category_id=food_cat.id)
    session.commit()

    s = trends.month_summary("2026-01")
    assert s.income_cents == 200000
    assert s.spent_cents == 6500
    assert s.budgeted_cents == 0


def test_month_summary_balance_allocated_minus_spent(trends: TrendsService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    _budget(session, food.id, "2026-01", allocated=10000, carryover=False)
    make_transaction(session, acc.id, amount_cents=-3000, date="2026-01-10", category_id=food.id)
    session.commit()

    s = trends.month_summary("2026-01")
    food_row = next(r for r in s.per_category if r.category_id == food.id)
    assert food_row.budgeted == 10000
    assert food_row.spent == 3000
    assert food_row.carryover == 0
    assert food_row.balance == 7000


def test_carryover_computed_on_read_when_enabled(trends: TrendsService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    _budget(session, food.id, "2026-01", allocated=10000, carryover=True)
    make_transaction(session, acc.id, amount_cents=-3000, date="2026-01-10", category_id=food.id)
    _budget(session, food.id, "2026-02", allocated=10000, carryover=True)
    session.commit()

    s = trends.month_summary("2026-02")
    food_row = next(r for r in s.per_category if r.category_id == food.id)
    # Jan surplus 7000 carries into Feb: carryover=7000, balance = 10000 - 0 + 7000
    assert food_row.carryover == 7000
    assert food_row.balance == 17000


def test_carryover_disabled_means_no_carry(trends: TrendsService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    _budget(session, food.id, "2026-01", allocated=10000, carryover=False)
    make_transaction(session, acc.id, amount_cents=-3000, date="2026-01-10", category_id=food.id)
    _budget(session, food.id, "2026-02", allocated=10000, carryover=False)
    session.commit()

    s = trends.month_summary("2026-02")
    food_row = next(r for r in s.per_category if r.category_id == food.id)
    assert food_row.carryover == 0
    assert food_row.balance == 10000


def test_back_dated_insertion_propagates_forward(trends: TrendsService, session: Session) -> None:
    """AC9: a back-dated M-2 edit changes M's carryover on re-read (on-read proof)."""
    acc = make_account(session)
    food = make_category(session, "Food")
    _budget(session, food.id, "2026-01", allocated=10000, carryover=True)
    _budget(session, food.id, "2026-02", allocated=10000, carryover=True)
    _budget(session, food.id, "2026-03", allocated=10000, carryover=True)
    session.commit()

    before = trends.month_summary("2026-03")
    assert next(r for r in before.per_category if r.category_id == food.id).carryover == 20000

    # Back-date a spend into January (M-2 relative to March).
    make_transaction(session, acc.id, amount_cents=-4000, date="2026-01-15", category_id=food.id)
    session.commit()

    after = trends.month_summary("2026-03")
    food_after = next(r for r in after.per_category if r.category_id == food.id)
    # Jan surplus drops from 10000 to 6000; Feb stays 10000; carry into Mar = 16000.
    assert food_after.carryover == 16000


def test_mom_deltas_returns_n_months(trends: TrendsService, session: Session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    food = make_category(session, "Food")
    make_transaction(session, acc.id, amount_cents=100000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-2000, date="2026-01-06", category_id=food.id)
    make_transaction(session, acc.id, amount_cents=-3000, date="2026-02-06", category_id=food.id)
    session.commit()

    deltas = trends.mom_deltas("2026-02", n=2)
    months = {m for m, _i, _s in deltas}
    assert months == {"2026-01", "2026-02"}
    by_month = {m: (i, s) for m, i, s in deltas}
    assert by_month["2026-01"] == (100000, 2000)
    assert by_month["2026-02"] == (0, 3000)


def test_trends_engine_has_no_llm_imports() -> None:
    from local_ledger.ledger import trends

    src = inspect.getsource(trends)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src, f"trends engine must not reference {token!r}"
