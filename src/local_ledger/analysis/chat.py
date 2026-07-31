"""Chat message persistence service."""

from __future__ import annotations

from sqlmodel import Session, select

from local_ledger.ledger import audit
from local_ledger.models import ChatMessage

HISTORY_LIMIT = 50


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
