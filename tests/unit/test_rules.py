"""Rules engine tests: matching, priority, backfill, no LLM."""

from __future__ import annotations

import inspect

import pytest
from sqlmodel import select

from local_ledger.automation.rules import RuleService
from local_ledger.db import init_db
from local_ledger.models import Rule, Transaction
from tests.factories import make_account, make_category


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


@pytest.fixture()
def svc(session) -> RuleService:  # type: ignore[no-untyped-def]
    return RuleService(session)


def _make_rule(svc: RuleService, **overrides) -> Rule:
    defaults = {
        "name": "Food rule",
        "payee_contains": "starbucks",
        "amount_min": None,
        "amount_max": None,
        "notes_contains": None,
        "category_id": 1,
        "priority": 10,
    }
    defaults.update(overrides)
    return svc.create_rule(**defaults)


def test_apply_rules_matches_payee(svc: RuleService, session) -> None:
    cat = make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks", category_id=cat.id)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks coffee")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) == cat.id


def test_apply_rules_priority_first_wins(svc: RuleService, session) -> None:
    food = make_category(session, "Food")
    coffee = make_category(session, "Coffee")
    _make_rule(svc, payee_contains="starbucks", category_id=coffee.id, priority=100)
    _make_rule(svc, payee_contains="starbucks", category_id=food.id, priority=10)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) == coffee.id


def test_apply_rules_amount_range(svc: RuleService, session) -> None:
    cat = make_category(session, "Food")
    _make_rule(svc, payee_contains="grocery", amount_min=1000, amount_max=5000, category_id=cat.id)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-2500, notes="grocery run")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) == cat.id


def test_apply_rules_no_match_returns_none(svc: RuleService, session) -> None:
    make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks")
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="other store")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) is None


def test_apply_rules_unknown_category_skipped(svc: RuleService, session) -> None:
    """A rule pointing to a deleted category is skipped, not fatal."""
    make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks", category_id=999)
    tx = Transaction(account_id=1, date="2026-01-05", amount_cents=-500, notes="starbucks")
    rules = svc.list_rules()
    assert svc.apply_rules_to_transaction(tx, rules) is None


def test_apply_rules_to_uncategorized_backfills(svc: RuleService, session) -> None:
    acc = make_account(session)
    cat = make_category(session, "Food")
    _make_rule(svc, payee_contains="starbucks", category_id=cat.id)
    session.add(
        Transaction(account_id=acc.id, date="2026-01-05", amount_cents=-500, notes="starbucks")
    )
    session.commit()
    rules = svc.list_rules()
    count = svc.apply_rules_to_uncategorized(rules)
    assert count == 1
    tx = session.exec(select(Transaction)).first()
    assert tx.category_id == cat.id


def test_rules_module_has_no_llm_imports() -> None:
    from local_ledger.automation import rules

    src = inspect.getsource(rules)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
