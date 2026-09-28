"""Paste-parse: turn pasted statement/budget text into reviewable transactions.

The LLM proposes transactions via structured output; the service NEVER writes to
the ledger during parse. The user reviews the proposals and ``commit_proposals``
writes only the accepted subset, atomically - through the same staging, dedup,
review and audit path as a CSV import, so pasted text gets the same duplicate
protection, the same provenance and the same audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from sqlmodel import Session

from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import CommitResult, ImportService
from local_ledger.ingest.payees import resolve_category
from local_ledger.llm.structured import StructuredOutputError, extract
from local_ledger.models import DedupStatus, Transaction


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
        batch_label: str = "paste",
    ) -> list[Transaction]:
        """Stage the accepted proposals and commit them, returning the new rows.

        Thin wrapper over :meth:`commit_accepted` for callers that only need the
        written transactions; use the latter to see what was held back.
        """
        return self.commit_accepted(
            proposals, account_id=account_id, actor=actor, batch_label=batch_label
        ).created

    def commit_accepted(
        self,
        proposals: list[PasteParseProposal],
        *,
        account_id: int,
        actor: str = "user",
        batch_label: str = "paste",
    ) -> CommitResult:
        """Route proposals through staging, then commit what needs no decision.

        The rows the caller hands over are the accepted subset, so rows the
        dedup engine called NEW are accepted explicitly here. A row that is
        FUZZY is *not*: it matched something already in the ledger, and the
        paste editor does not show the user that, so it goes to the review
        queue and is reported as ``needs-acceptance`` instead of being written
        behind the user's back. EXACT rows are already in the ledger and are
        skipped.

        Payee and suggested category survive into the staged row; a category
        name that matches nothing stays unresolved rather than creating a new
        budget category.
        """
        importer = ImportService(self.session)
        parsed = [
            ParsedTransaction(
                date=p.date,
                amount_cents=p.amount_cents,
                payee_raw=p.payee,
                imported_id=None,
            )
            for p in proposals
        ]
        staged = importer.stage(parsed, account_id, batch_label=batch_label)
        # Category ids live on the staged row. An unmatched name leaves the row's
        # category unset instead of creating a budget category the user never had.
        for row, proposal in zip(staged, proposals, strict=True):
            category = resolve_category(self.session, proposal.suggested_category)
            if category is not None:
                row.suggested_category_id = category.id
                self.session.add(row)
        if any(r.suggested_category_id is not None for r in staged):
            self.session.commit()
        ids = [s.id for s in staged if s.id is not None]
        undecided = [s.id for s in staged if s.dedup_status is not DedupStatus.NEW]
        if ids:
            importer.accept([i for i in ids if i not in set(undecided)])
        return importer.commit(
            ids,
            actor=actor,
            account_id=account_id,
            batch=staged[0].import_batch if staged else None,
        )
