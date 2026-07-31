"""AppTest for enhanced transactions page."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Transaction
from tests.factories import make_category

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "txns.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        svc = LedgerService(s)
        acc = svc.create_account(name="Checking", type="checking")
        food = make_category(s, "Food")
        s.add(Transaction(account_id=acc.id, date="2026-01-05", amount_cents=-500, notes="starbucks", category_id=food.id))
        s.add(Transaction(account_id=acc.id, date="2026-01-06", amount_cents=-2000, notes="grocery", category_id=food.id))
        s.commit()
    engine.dispose()
    return url


def test_transactions_page_renders_with_enhancements(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/transactions.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"transactions page raised: {at.exception}"
