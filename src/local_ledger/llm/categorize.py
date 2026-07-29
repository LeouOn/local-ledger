"""Auto-categorization: LLM proposes categories for uncategorized transactions.

Dry-run by default. Low-confidence (<0.7) or unknown-category proposals are
flagged for review. ``apply_proposals`` writes only known category IDs with an
``llm:<provider>:<model>`` audit actor - categories are NEVER created silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session, select

from local_ledger.db import transaction_with_retry
from local_ledger.ledger import audit
from local_ledger.llm.structured import StructuredOutputError, extract
from local_ledger.models import Category, Transaction

CONFIDENCE_REVIEW_THRESHOLD = 0.7
DEFAULT_BATCH_SIZE = 50


class _ProposalItem(BaseModel):
    transaction_id: int
    category_id: int
    confidence: float


class _ProposalBatch(BaseModel):
    proposals: list[_ProposalItem]


@dataclass
class CategorizationProposal:
    transaction_id: int
    category_id: int
    confidence: float
    needs_review: bool = False


class CategorizationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def propose_categories(
        self,
        *,
        tx_ids: list[int],
        categories: list[Category],
        completer,
        model: str,
        dry_run: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = 2,
    ) -> list[CategorizationProposal]:
        known_ids = {c.id for c in categories}
        category_menu = ", ".join(f"{c.id}:{c.name}" for c in categories)
        proposals: list[CategorizationProposal] = []

        for start in range(0, len(tx_ids), batch_size):
            chunk = tx_ids[start : start + batch_size]
            txs = list(self.session.exec(select(Transaction).where(Transaction.id.in_(chunk))).all())
            tx_lines = "; ".join(f"{t.id}|{t.amount_cents}|{t.notes or ''}" for t in txs)
            messages = [
                {
                    "role": "system",
                    "content": (
                        "Assign each transaction a category_id from the menu and a confidence. "
                        'Return JSON {"proposals": [{"transaction_id": int, "category_id": int, '
                        '"confidence": float}]}. Use only the listed category_ids.'
                    ),
                },
                {"role": "user", "content": f"menu: {category_menu}\ntransactions: {tx_lines}"},
            ]
            try:
                parsed = extract(completer, _ProposalBatch, messages, model, max_retries=max_retries)
            except StructuredOutputError:
                # On parse failure, flag all chunk txs for review rather than crash.
                for tid in chunk:
                    proposals.append(CategorizationProposal(tid, -1, 0.0, needs_review=True))
                continue
            for item in parsed.proposals:
                needs_review = (
                    item.confidence < CONFIDENCE_REVIEW_THRESHOLD or item.category_id not in known_ids
                )
                proposals.append(
                    CategorizationProposal(
                        transaction_id=item.transaction_id,
                        category_id=item.category_id,
                        confidence=item.confidence,
                        needs_review=needs_review,
                    )
                )
        if not dry_run:
            self.apply_proposals(
                [p for p in proposals if not p.needs_review],
                actor=f"llm:{model}",
            )
        return proposals

    def apply_proposals(self, proposals: list[CategorizationProposal], *, actor: str) -> None:
        known_ids = {
            c.id for c in list(self.session.exec(select(Category)).all())
        }

        def _op() -> None:
            for p in proposals:
                if p.category_id not in known_ids:
                    raise ValueError(
                        f"refusing to apply unknown category_id {p.category_id} "
                        "(categories are never created silently)"
                    )
                tx = self.session.get(Transaction, p.transaction_id)
                if tx is None:
                    continue
                before = tx.model_copy()
                tx.category_id = p.category_id
                self.session.add(tx)
                self.session.flush()
                audit.record(
                    self.session,
                    entity_type="transaction",
                    entity_id=tx.id,
                    action="update",
                    actor=actor,
                    before=before,
                    after=tx,
                )
            self.session.commit()

        transaction_with_retry(_op)
