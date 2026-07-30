"""Recommendation store: append-only bodies, user-only ``set_followed``.

A recommendation's body is write-once. The only mutator is ``set_followed``,
which is reserved for the user (LLM code has no path to it). Follow-up reports
recompute measured deltas from the trends engine rather than trusting stored
numbers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlmodel import Session, select

from local_ledger.db import transaction_with_retry
from local_ledger.ledger import audit
from local_ledger.models import Recommendation


@dataclass
class FollowupItem:
    recommendation_id: int
    month: str
    body: str
    followed: bool | None
    current_month_spent_cents: int | None = None
    current_month_budgeted_cents: int | None = None


class RecommendationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        *,
        month: str,
        provider: str,
        model: str,
        prompt_hash: str,
        body: str,
    ) -> Recommendation:
        rec = Recommendation(
            month=month,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            body=body,
        )

        def _op() -> Recommendation:
            self.session.add(rec)
            self.session.flush()
            audit.record(
                self.session,
                entity_type="recommendation",
                entity_id=rec.id,
                action="create",
                actor=f"llm:{provider}:{model}",
                after=rec,
            )
            self.session.commit()
            return rec

        return transaction_with_retry(_op)

    def set_followed(self, recommendation_id: int, followed: bool, *, actor: str = "user") -> Recommendation:
        if actor != "user":
            raise PermissionError("only the user may set the followed flag")

        def _op() -> Recommendation:
            rec = self.session.get(Recommendation, recommendation_id)
            if rec is None:
                raise ValueError(f"no recommendation {recommendation_id}")
            before = rec.model_copy()
            rec.followed = followed
            self.session.add(rec)
            self.session.flush()
            audit.record(
                self.session,
                entity_type="recommendation",
                entity_id=rec.id,
                action="update",
                actor="user",
                before=before,
                after=rec,
            )
            self.session.commit()
            return rec

        return transaction_with_retry(_op)

    def list_for_month(self, month: str) -> list[Recommendation]:
        return list(self.session.exec(select(Recommendation).where(Recommendation.month == month)).all())

    def followup_report(self, *, current_month: str, trends_factory=None) -> list[FollowupItem]:  # type: ignore[no-untyped-def]
        prior = list(
            self.session.exec(select(Recommendation).where(Recommendation.month < current_month)).all()
        )
        spent = budgeted = None
        if trends_factory is not None:
            try:
                summary = trends_factory(current_month)
                spent = summary.spent_cents
                budgeted = summary.budgeted_cents
            except Exception:
                pass
        return [
            FollowupItem(
                recommendation_id=r.id,
                month=r.month,
                body=r.body,
                followed=r.followed,
                current_month_spent_cents=spent,
                current_month_budgeted_cents=budgeted,
            )
            for r in prior
        ]

    def diff_view(self, *, month_a: str, month_b: str) -> dict[str, list[str]]:
        return {
            month_a: [r.body for r in self.list_for_month(month_a)],
            month_b: [r.body for r in self.list_for_month(month_b)],
        }

    @staticmethod
    def hash_prompt(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
