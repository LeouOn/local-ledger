"""Rule model tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ledger.models import Rule


def test_rule_constructs() -> None:
    rule = Rule(
        name="Starbucks → Food",
        payee_contains="starbucks",
        amount_min=None,
        amount_max=None,
        notes_contains=None,
        category_id=1,
        priority=10,
    )
    assert rule.name == "Starbucks → Food"
    assert rule.payee_contains == "starbucks"
    assert rule.category_id == 1
    assert rule.priority == 10


def test_rule_requires_name() -> None:
    with pytest.raises(ValidationError):
        Rule(
            name="",
            payee_contains="x",
            amount_min=None,
            amount_max=None,
            notes_contains=None,
            category_id=1,
            priority=10,
        )


def test_rule_negative_amount_rejected() -> None:
    with pytest.raises(ValidationError):
        Rule(
            name="bad",
            payee_contains="x",
            amount_min=-5,
            amount_max=None,
            notes_contains=None,
            category_id=1,
            priority=10,
        )
