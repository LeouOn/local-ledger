"""AppTest-driven end-to-end journey (Oracle finding #7).

Boots the full app.py (with navigation) against a temp DB and verifies it
renders without exception — the genuine Streamlit surface, not the
service-level pipeline tested in tests/unit/test_e2e_journey.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.ledger.service import LedgerService
from local_ledger.models import CategoryBudget
from tests.factories import make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "journey.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    from local_ledger.db import run_migrations

    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        svc = LedgerService(s)
        acc = svc.create_account(name="Checking", type="checking")
        food = svc.create_category(name="Food")
        for i in range(5):
            make_transaction(s, acc.id, amount_cents=-(1000 + i * 100), date="2026-01-10", category_id=food.id)
        s.add(CategoryBudget(category_id=food.id, month="2026-01", allocated_cents=10000))
        s.commit()
    engine.dispose()
    return url


def test_app_boots_and_renders_home(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"app raised: {at.exception}"


def test_app_boots_with_empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The app must boot cleanly against a fresh empty DB (first-run scenario)."""
    db = tmp_path / "empty.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"app raised on empty DB: {at.exception}"
