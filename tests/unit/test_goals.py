"""GoalService tests: progress, projection, on-track logic."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from local_ledger.analysis.goals import GoalService
from local_ledger.db import init_db
from local_ledger.models import SavingsGoal
from tests.factories import make_account, make_category


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


@pytest.fixture()
def service(session) -> GoalService:
    return GoalService(session)


def _make_goal(session, **overrides) -> SavingsGoal:
    defaults = {
        "name": "Emergency Fund",
        "target_cents": 1000000,
        "target_date": "2026-12-31",
        "monthly_contribution_cents": 50000,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    goal = SavingsGoal(**defaults)
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return goal


def test_create_goal(service: GoalService, session) -> None:
    goal = service.create_goal("Vacation", 200000, "2026-06-30", 30000)
    assert goal.id is not None
    assert goal.name == "Vacation"


def test_current_progress_computes_from_ledger(service: GoalService, session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    from tests.factories import make_transaction

    make_transaction(session, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(session, target_cents=1000000)
    progress = service.current_progress(goal)
    assert progress.current_cents == 400000  # income - expenses
    assert progress.target_cents == 1000000
    assert progress.pct == 40.0


def test_projected_achievement(service: GoalService, session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    from tests.factories import make_transaction

    make_transaction(session, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(session, target_cents=1000000, target_date="2026-12-31")
    achievement = service.projected_achievement(goal)
    assert achievement is not None
    assert achievement > "2026-01-01"


def test_on_track_true_when_ahead(service: GoalService, session) -> None:
    acc = make_account(session)
    income = make_category(session, "Salary", is_income=True)
    from tests.factories import make_transaction

    make_transaction(session, acc.id, amount_cents=1000000, date="2026-01-05", category_id=income.id)
    make_transaction(session, acc.id, amount_cents=-100000, date="2026-01-10")
    session.commit()

    goal = _make_goal(
        session, target_cents=500000, target_date="2026-12-31", monthly_contribution_cents=40000
    )
    assert service.on_track(goal) is True


def test_on_track_false_when_behind(service: GoalService, session) -> None:
    goal = _make_goal(
        session, target_cents=1000000, target_date="2026-02-28", monthly_contribution_cents=10000
    )
    assert service.on_track(goal) is False


def test_zero_savings_rate_never_achieves(service: GoalService, session) -> None:
    goal = _make_goal(session, target_cents=1000000, target_date="2026-12-31")
    assert service.projected_achievement(goal) is None


def test_list_and_delete_goals(service: GoalService, session) -> None:
    g1 = service.create_goal("A", 100000, "2026-12-31", 10000)
    g2 = service.create_goal("B", 200000, "2026-12-31", 20000)
    goals = service.list_goals()
    assert len(goals) == 2
    service.delete_goal(g1.id)
    assert len(service.list_goals()) == 1


def test_goals_module_has_no_llm_imports() -> None:
    import inspect

    from local_ledger.analysis import goals

    src = inspect.getsource(goals)
    for token in ("openai", "ollama", "instructor"):
        assert token not in src
