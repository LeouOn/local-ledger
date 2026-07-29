"""Paste-parse tests: LLM tool-calling parse → confirm-before-commit.

Uses an injected completer (no real LLM) for determinism. The service never
writes to the ledger during parse; only ``commit_proposals`` writes, atomically.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.llm.paste_parse import ParseFailure, PasteParseProposal, PasteParseService
from local_ledger.models import Transaction
from tests.factories import make_account


@pytest.fixture()
def svc(session: Session, engine) -> PasteParseService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return PasteParseService(session)


def _completer_returning(payload):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        return payload
    return _c


def test_parse_returns_proposals_without_writing(svc: PasteParseService, session: Session) -> None:
    payload = json.dumps(
        {
            "transactions": [
                {"date": "2026-01-05", "payee": "Coffee", "amount_cents": -450, "suggested_category": "Food"},
                {"date": "2026-01-06", "payee": "Bus", "amount_cents": -250, "suggested_category": "Transport"},
            ]
        }
    )
    proposals = svc.parse_pasted_text("paste", completer=_completer_returning(payload), model="m")
    assert len(proposals) == 2
    assert proposals[0].amount_cents == -450
    # nothing committed yet
    assert len(list(session.exec(select(Transaction)).all())) == 0


def test_commit_proposals_writes_atomically(svc: PasteParseService, session: Session) -> None:
    acc = make_account(session)
    session.commit()
    proposals = [
        PasteParseProposal(date="2026-01-05", amount_cents=-450, payee="Coffee", suggested_category="Food"),
        PasteParseProposal(date="2026-01-06", amount_cents=-250, payee="Bus", suggested_category="Transport"),
        PasteParseProposal(date="2026-01-07", amount_cents=100000, payee="Payroll", suggested_category="Income"),
    ]
    accepted = proposals[:2]  # user rejects the 3rd
    txns = svc.commit_proposals(accepted, account_id=acc.id, actor="user")
    assert len(txns) == 2
    assert len(list(session.exec(select(Transaction)).all())) == 2


def test_parse_failure_raises_typed_error(svc: PasteParseService) -> None:
    """LLM returns no valid transactions -> typed ParseFailure, zero writes."""
    with pytest.raises(ParseFailure):
        svc.parse_pasted_text("paste", completer=_completer_returning("garbage"), model="m")


def test_large_input_is_chunked(svc: PasteParseService) -> None:
    """Inputs over the chunk cap are split into multiple completer calls."""
    big = "x" * 25_000
    calls = {"n": 0}

    def comp(_m, _model):
        calls["n"] += 1
        return json.dumps({"transactions": []})

    with pytest.raises(ParseFailure):
        svc.parse_pasted_text(big, completer=comp, model="m", chunk_chars=10_000)
    assert calls["n"] >= 3  # 25k / 10k -> at least 3 chunks


def test_parse_never_commits_on_partial_failure(svc: PasteParseService, session: Session) -> None:
    """A parse that yields no proposals must leave the ledger untouched."""
    session.commit()
    with pytest.raises(ParseFailure):
        svc.parse_pasted_text("paste", completer=_completer_returning('{"transactions": []}'), model="m")
    assert len(list(session.exec(select(Transaction)).all())) == 0
