"""AppTest for the chat sidebar."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import run_migrations
from local_ledger.ledger.service import LedgerService
from tests.factories import make_transaction

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "chat_sidebar.db"
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
        income = svc.create_category(name="Salary", is_income=True)
        make_transaction(s, acc.id, amount_cents=500000, date="2026-01-05", category_id=income.id)
    engine.dispose()
    return url


def test_chat_sidebar_renders_in_app(seeded_db: str) -> None:
    """The app.py entry should render without exception, which now includes the chat sidebar."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception, f"app with chat sidebar raised: {at.exception}"


def test_app_py_wires_chat_sidebar() -> None:
    """app.py must import and call render_chat_sidebar."""
    app_src = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "from local_ledger.ui.chat_sidebar import" in app_src
    assert "render_chat_sidebar(" in app_src


def test_render_chat_sidebar_module_exists() -> None:
    """The chat_sidebar module must export render_chat_sidebar."""
    from local_ledger.ui import chat_sidebar

    assert hasattr(chat_sidebar, "render_chat_sidebar")
    assert callable(chat_sidebar.render_chat_sidebar)
