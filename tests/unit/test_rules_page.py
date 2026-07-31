"""AppTest for the Rules page."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from tests.factories import make_category

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "rules.db"
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
        svc.create_account(name="Checking", type="checking")
        make_category(s, "Food")
    engine.dispose()
    return url


def test_rules_page_renders(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app_pages/rules.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"rules page raised: {at.exception}"
