"""Paste-parse: turn pasted statement/budget text into reviewable transactions.

The LLM proposes transactions via structured output; the service NEVER writes to
the ledger during parse. The user reviews the proposals and ``commit_proposals``
writes only the accepted subset, atomically, via the import pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session

from local_ledger.db import transaction_with_retry
from local_ledger.ledger import audit
from local_ledger.llm.structured import StructuredOutputError, extract
from local_ledger.models import Transaction


class _ParsedItem(BaseModel):
    date: str
    payee: str | None = None
    amount_cents: int
    suggested_category: str | None = None


class _ParsedBatch(BaseModel):
    transactions: list[_ParsedItem]


@dataclass
class PasteParseProposal:
    date: str
    amount_cents: int
    payee: str | None = None
    suggested_category: str | None = None


class ParseFailure(Exception):
    pass


_DEFAULT_CHUNK = 20_000


class PasteParseService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def parse_pasted_text(
        self,
        text: str,
        *,
        completer,
        model: str,
        chunk_chars: int = _DEFAULT_CHUNK,
        max_retries: int = 2,
    ) -> list[PasteParseProposal]:
        proposals: list[PasteParseProposal] = []
        chunks = [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)] or [text]
        for chunk in chunks:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Extract transactions from the user's pasted statement text as JSON "
                        'with shape {"transactions": [{"date":"YYYY-MM-DD","payee":str,'
                        '"amount_cents":int,"suggested_category":str}]}. Expenses are negative, '
                        "income is positive."
                    ),
                },
                {"role": "user", "content": chunk},
            ]
            try:
                parsed = extract(completer, _ParsedBatch, messages, model, max_retries=max_retries)
            except StructuredOutputError as exc:
                raise ParseFailure(f"LLM failed to parse chunk: {exc}") from exc
            for item in parsed.transactions:
                proposals.append(
                    PasteParseProposal(
                        date=item.date,
                        amount_cents=item.amount_cents,
                        payee=item.payee,
                        suggested_category=item.suggested_category,
                    )
                )
        if not proposals:
            raise ParseFailure("no transactions parsed from pasted text")
        return proposals

    def commit_proposals(
        self,
        proposals: list[PasteParseProposal],
        *,
        account_id: int,
        actor: str = "user",
    ) -> list[Transaction]:
        def _op() -> list[Transaction]:
            created: list[Transaction] = []
            for p in proposals:
                tx = Transaction(
                    account_id=account_id,
                    date=p.date,
                    amount_cents=p.amount_cents,
                    created_by=actor,
                )
                self.session.add(tx)
                self.session.flush()
                audit.record(
                    self.session,
                    entity_type="transaction",
                    entity_id=tx.id,
                    action="create",
                    actor=actor,
                    after=tx,
                )
                created.append(tx)
            self.session.commit()
            return created

        return transaction_with_retry(_op)
