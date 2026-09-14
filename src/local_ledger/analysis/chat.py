"""Chat message persistence service + shared aggregates-only chat context."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.ledger import audit
from local_ledger.ledger.trends import TrendsService
from local_ledger.models import CategoryBudget, ChatMessage, Transaction

HISTORY_LIMIT = 50


def build_chat_context(session: Session, month: str) -> dict:
    """Build the aggregates-only chat context for a month.

    Every page that knows the user's selected month injects this into the
    sidebar so the LLM always sees fresh totals, never stale ones.
    """
    summary = TrendsService(session).month_summary(month)
    lines = [
        f"Month {month}: income ${summary.income_cents / 100:,.0f}, "
        f"spent ${summary.spent_cents / 100:,.0f}, "
        f"budgeted ${summary.budgeted_cents / 100:,.0f}."
    ]
    for c in summary.per_category:
        lines.append(
            f"  {c.name}: budgeted ${c.budgeted / 100:,.0f}, "
            f"spent ${c.spent / 100:,.0f}, balance ${c.balance / 100:,.0f}."
        )
    return {"month": month, "summary_text": "\n".join(lines)}


def latest_data_month(session: Session) -> str | None:
    """Most recent month with any transaction or budget row, if any."""
    months: set[str] = {t.date[:7] for t in session.exec(select(Transaction)).all()}
    months |= {b.month for b in session.exec(select(CategoryBudget)).all()}
    return max(months) if months else None


def refresh_chat_context(session: Session, context: dict | None) -> dict | None:
    """Rebuild chat aggregates from the DB, preserving the selected month.

    A remembered context can hold totals that went stale after an import or
    an edit, so only its ``month`` is trusted; the aggregates themselves are
    always recomputed. With no month remembered, fall back to the latest
    month that has data (None when the ledger is empty).
    """
    month = (context or {}).get("month") or latest_data_month(session)
    if month is None:
        return None
    return build_chat_context(session, month)


class ChatService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_history(self, limit: int = HISTORY_LIMIT) -> list[ChatMessage]:
        msgs = list(
            self.session.exec(
                select(ChatMessage)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(limit)
            ).all()
        )
        return list(reversed(msgs))

    def add_message(self, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(role=role, content=content)
        self.session.add(msg)
        self.session.flush()
        audit.record(
            self.session,
            entity_type="chat_message",
            entity_id=msg.id,
            action="create",
            actor="user",
            after=msg,
        )
        self.session.commit()
        self.session.refresh(msg)
        return msg

    def clear_history(self) -> None:
        for msg in self.session.exec(select(ChatMessage)).all():
            audit.record(
                self.session,
                entity_type="chat_message",
                entity_id=msg.id,
                action="delete",
                actor="user",
                before=msg,
            )
            self.session.delete(msg)
        self.session.commit()
