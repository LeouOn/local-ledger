"""Phase 2 model tests: SavingsGoal + ChatMessage."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ledger.models import ChatMessage, SavingsGoal


def test_savings_goal_constructs() -> None:
    goal = SavingsGoal(
        name="Emergency Fund",
        target_cents=1000000,
        target_date="2026-12-31",
        monthly_contribution_cents=50000,
    )
    assert goal.name == "Emergency Fund"
    assert goal.target_cents == 1000000
    assert goal.target_date == "2026-12-31"


def test_savings_goal_requires_positive_target() -> None:
    with pytest.raises(ValidationError):
        SavingsGoal(name="Bad", target_cents=0, target_date="2026-12-31", monthly_contribution_cents=100)


def test_chat_message_constructs() -> None:
    msg = ChatMessage(role="user", content="Why is food so high?")
    assert msg.role == "user"
    assert msg.content == "Why is food so high?"


def test_chat_message_rejects_invalid_role() -> None:
    with pytest.raises(ValidationError):
        ChatMessage(role="invalid", content="test")
