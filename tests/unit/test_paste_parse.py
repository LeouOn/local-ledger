"""Paste-parse tests: LLM parse → review → commit through the import pipeline.

Uses an injected completer (no real LLM) for determinism. The service never
writes to the ledger during parse; ``commit_proposals`` stages the accepted
subset and commits it through ``ImportService``, so pasted text gets the same
dedup, provenance, review and audit behaviour as a CSV import.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.ingest.csv_import import ParsedTransaction
from local_ledger.ingest.importer import SKIP_NEEDS_ACCEPTANCE, ImportService
from local_ledger.llm.paste_parse import ParseFailure, PasteParseProposal, PasteParseService
from local_ledger.models import Payee, StagedTransaction, Transaction
from tests.factories import make_account, make_category


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


def test_commit_proposals_keeps_payee_and_matching_category(
    svc: PasteParseService, session: Session
) -> None:
    """Payee and an existing category survive; an unknown category is not invented."""
    acc = make_account(session)
    cat = make_category(session, name="Food")
    session.commit()
    proposals = [
        PasteParseProposal(date="2026-01-05", amount_cents=-450, payee="Coffee", suggested_category="Food"),
        PasteParseProposal(date="2026-01-06", amount_cents=-250, payee="Bus", suggested_category="Fictional"),
    ]

    svc.commit_proposals(proposals, account_id=acc.id, actor="user")

    txs = sorted(list(session.exec(select(Transaction)).all()), key=lambda t: t.date)
    assert txs[0].category_id == cat.id
    assert txs[1].category_id is None
    payees = {p.name for p in session.exec(select(Payee)).all()}
    assert {"Coffee", "Bus"} <= payees
    # The invented category was never created.
    from local_ledger.models import Category

    assert [c.name for c in session.exec(select(Category)).all()] == ["Food"]


def test_commit_proposals_records_staged_provenance(
    svc: PasteParseService, session: Session
) -> None:
    acc = make_account(session)
    session.commit()
    proposals = [PasteParseProposal(date="2026-01-05", amount_cents=-450, payee="Coffee")]

    txns = svc.commit_proposals(proposals, account_id=acc.id, actor="user", batch_label="paste-1")

    staged = list(session.exec(select(StagedTransaction)).all())
    assert len(staged) == 1
    assert staged[0].import_batch == "paste-1"
    assert staged[0].committed_transaction_id == txns[0].id


def test_commit_proposals_twice_writes_once(svc: PasteParseService, session: Session) -> None:
    """Re-pasting the same text asks instead of duplicating: the second row is FUZZY."""
    acc = make_account(session)
    session.commit()
    proposals = [PasteParseProposal(date="2026-01-05", amount_cents=-450, payee="Coffee")]

    first = svc.commit_proposals(proposals, account_id=acc.id, actor="user")
    result = svc.commit_accepted(proposals, account_id=acc.id, actor="user")

    assert len(first) == 1
    assert result.created == []
    assert [s.reason for s in result.skipped] == [SKIP_NEEDS_ACCEPTANCE]
    assert len(list(session.exec(select(Transaction)).all())) == 1
    # The held-back row is waiting in the review queue, not lost.
    assert len(ImportService(session).list_staged()) == 1


def test_accepted_paste_rows_are_visible_as_reviewed_staged_rows(
    svc: PasteParseService, session: Session
) -> None:
    """The paste path uses the same review state the Import page shows."""
    acc = make_account(session)
    session.commit()
    svc.commit_accepted(
        [PasteParseProposal(date="2026-01-05", amount_cents=-450, payee="Coffee")],
        account_id=acc.id,
        actor="user",
    )
    staged = list(session.exec(select(StagedTransaction)).all())
    assert all(s.review_state == "ACCEPTED" for s in staged)
    assert ImportService(session).list_staged() == []


def test_unaccepted_paste_row_never_reaches_the_ledger(
    svc: PasteParseService, session: Session
) -> None:
    """Guard the review rule from the other side: a PENDING row is not committable."""
    acc = make_account(session)
    session.commit()
    importer = ImportService(session)
    staged = importer.stage(
        [ParsedTransaction(date="2026-01-05", amount_cents=-450, payee_raw="Coffee")],
        acc.id,
        "paste-2",
    )
    result = importer.commit([staged[0].id], actor="user")
    assert result.skipped_reasons == {staged[0].id: SKIP_NEEDS_ACCEPTANCE}
    assert list(session.exec(select(Transaction)).all()) == []


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
