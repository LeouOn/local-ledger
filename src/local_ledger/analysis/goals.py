"""Goal tracking service: progress, projection, on-track logic. No LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from local_ledger.ledger import audit
from local_ledger.models import SavingsGoal, Transaction


@dataclass
class GoalProgress:
    current_cents: int
    target_cents: int
    pct: float
    monthly_rate_cents: float
    planned_rate_cents: float
    behind_plan: bool


class GoalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_goal(
        self,
        name: str,
        target_cents: int,
        target_date: str,
        monthly_contribution_cents: int,
        notes: str | None = None,
    ) -> SavingsGoal:
        goal = SavingsGoal(
            name=name,
            target_cents=target_cents,
            target_date=target_date,
            monthly_contribution_cents=monthly_contribution_cents,
            notes=notes,
        )
        self.session.add(goal)
        self.session.flush()
        audit.record(
            self.session,
            entity_type="savings_goal",
            entity_id=goal.id,
            action="create",
            actor="user",
            after=goal,
        )
        self.session.commit()
        self.session.refresh(goal)
        return goal

    def list_goals(self) -> list[SavingsGoal]:
        return list(
            self.session.exec(select(SavingsGoal).order_by(SavingsGoal.created_at)).all()
        )

    def delete_goal(self, goal_id: int) -> None:
        goal = self.session.get(SavingsGoal, goal_id)
        if goal is not None:
            audit.record(
                self.session,
                entity_type="savings_goal",
                entity_id=goal.id,
                action="delete",
                actor="user",
                before=goal,
            )
            self.session.delete(goal)
            self.session.commit()

    def current_progress(self, goal: SavingsGoal) -> GoalProgress:
        txns = list(self.session.exec(select(Transaction)).all())
        income = sum(t.amount_cents for t in txns if t.amount_cents > 0)
        expenses = sum(-t.amount_cents for t in txns if t.amount_cents < 0)
        current = income - expenses

        now = datetime.now(UTC)
        created = goal.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        months_elapsed = max(1.0, (now - created).days / 30.44)
        monthly_rate = current / months_elapsed
        pct = (current / goal.target_cents * 100) if goal.target_cents > 0 else 0.0

        return GoalProgress(
            current_cents=current,
            target_cents=goal.target_cents,
            pct=round(min(pct, 999.9), 1),
            monthly_rate_cents=round(monthly_rate),
            planned_rate_cents=goal.monthly_contribution_cents,
            behind_plan=goal.monthly_contribution_cents > round(monthly_rate),
        )

    def projected_achievement(self, goal: SavingsGoal) -> str | None:
        progress = self.current_progress(goal)
        if progress.monthly_rate_cents <= 0:
            return None
        remaining = goal.target_cents - progress.current_cents
        if remaining <= 0:
            return goal.target_date
        months_needed = remaining / progress.monthly_rate_cents
        days_needed = int(months_needed * 30.44)
        achievement = datetime.now(UTC) + timedelta(days=days_needed)
        return achievement.strftime("%Y-%m-%d")

    def on_track(self, goal: SavingsGoal) -> bool:
        achievement = self.projected_achievement(goal)
        if achievement is None:
            return False
        return achievement <= goal.target_date
