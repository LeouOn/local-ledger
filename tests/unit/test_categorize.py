"""Auto-categorization tests: dry-run default, confidence queue, llm audit, no silent category creation."""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.llm.categorize import CategorizationProposal, CategorizationService
from local_ledger.models import AuditLog, Category
from tests.factories import make_account, make_category, make_transaction


@pytest.fixture()
def svc(session: Session, engine) -> CategorizationService:  # type: ignore[no-untyped-def]
    init_db(engine)
    return CategorizationService(session)


def _completer_returning(items):  # type: ignore[no-untyped-def]
    def _c(_messages, _model):
        return json.dumps({"proposals": items})
    return _c


def test_dry_run_writes_nothing(svc: CategorizationService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    proposals = svc.propose_categories(
        tx_ids=[tx.id],
        categories=[food],
        completer=_completer_returning([{"transaction_id": tx.id, "category_id": food.id, "confidence": 0.9}]),
        model="m",
        dry_run=True,
    )
    assert len(proposals) == 1
    assert proposals[0].confidence == 0.9
    # dry-run: category unchanged, no audit row
    session.refresh(tx)
    assert tx.category_id is None
    assert len(list(session.exec(select(AuditLog)).all())) == 0


def test_apply_writes_with_llm_audit_actor(svc: CategorizationService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    proposals = [
        CategorizationProposal(transaction_id=tx.id, category_id=food.id, confidence=0.9),
    ]
    svc.apply_proposals(proposals, actor="llm:ollama:llama3.3:8b")
    session.refresh(tx)
    assert tx.category_id == food.id
    audits = list(session.exec(select(AuditLog)).all())
    assert len(audits) == 1
    assert audits[0].actor == "llm:ollama:llama3.3:8b"
    assert audits[0].action == "update"


def test_low_confidence_flagged_for_review(svc: CategorizationService, session: Session) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    proposals = svc.propose_categories(
        tx_ids=[tx.id],
        categories=[food],
        completer=_completer_returning([{"transaction_id": tx.id, "category_id": food.id, "confidence": 0.4}]),
        model="m",
    )
    assert proposals[0].confidence < 0.7
    assert proposals[0].needs_review is True


def test_unknown_category_name_routed_to_review_not_created(
    svc: CategorizationService, session: Session
) -> None:
    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    # LLM proposes a category_id that does not exist
    proposals = svc.propose_categories(
        tx_ids=[tx.id],
        categories=[food],
        completer=_completer_returning([{"transaction_id": tx.id, "category_id": 99999, "confidence": 0.95}]),
        model="m",
    )
    assert proposals[0].needs_review is True
    # no category was created
    assert len(list(session.exec(select(Category)).all())) == 1


def test_apply_only_accepts_known_categories(svc: CategorizationService, session: Session) -> None:
    """apply_proposals refuses to write an unknown category_id (no silent creation)."""
    acc = make_account(session)
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    with pytest.raises(ValueError):
        svc.apply_proposals(
            [CategorizationProposal(transaction_id=tx.id, category_id=99999, confidence=0.99)],
            actor="llm:m",
        )
    session.refresh(tx)
    assert tx.category_id is None


def test_batch_size_cap(svc: CategorizationService, session: Session) -> None:
    """A batch larger than the cap is split into multiple completer calls."""
    acc = make_account(session)
    food = make_category(session, "Food")
    txs = [make_transaction(session, acc.id, amount_cents=-i, category_id=None) for i in range(1, 11)]
    session.commit()

    calls = {"n": 0}

    def comp(_messages, _model):
        calls["n"] += 1
        return json.dumps({"proposals": []})

    svc.propose_categories(
        tx_ids=[t.id for t in txs],
        categories=[food],
        completer=comp,
        model="m",
        batch_size=4,
    )
    assert calls["n"] == 3  # ceil(10/4)


def test_cloud_path_blocked(svc: CategorizationService, session: Session) -> None:
    """Categorization requires payee data which cannot leave the machine."""
    from local_ledger.llm import providers

    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(session, acc.id, amount_cents=-500, category_id=None)
    session.commit()

    with pytest.raises(providers.CloudCallBlocked):
        svc.propose_categories(
            tx_ids=[tx.id],
            categories=[food],
            completer=lambda _m, _mo: json.dumps({"proposals": []}),
            model="m",
            provider="cloud",
        )


def test_prompt_does_not_contain_raw_notes(svc: CategorizationService, session: Session) -> None:
    """The categorize prompt must never include raw transaction notes."""
    acc = make_account(session)
    food = make_category(session, "Food")
    tx = make_transaction(
        session, acc.id, amount_cents=-500, category_id=None
    )
    tx.notes = "SECRET PERSONAL NOTE about Dr. John Smith at 123 Main St"
    session.add(tx)
    session.commit()

    captured = {}

    def capturing_completer(messages, _model):
        captured["prompt"] = messages[1]["content"]
        return json.dumps({"proposals": [{"transaction_id": tx.id, "category_id": food.id, "confidence": 0.9}]})

    svc.propose_categories(
        tx_ids=[tx.id],
        categories=[food],
        completer=capturing_completer,
        model="m",
    )
    prompt = captured.get("prompt", "")
    assert "SECRET PERSONAL NOTE" not in prompt
    assert "Dr. John Smith" not in prompt
    assert "123 Main St" not in prompt
