"""AppTest for the new Insights page sections (trend narrative + what-if)."""

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
    db = tmp_path / "insights.db"
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
        for m in range(1, 4):
            make_transaction(
                s,
                acc.id,
                amount_cents=-(10000 + m * 5000),
                date=f"2026-0{m}-10",
                category_id=food.id,
            )
        s.add(CategoryBudget(category_id=food.id, month="2026-01", allocated_cents=30000))
        s.add(CategoryBudget(category_id=food.id, month="2026-02", allocated_cents=30000))
        s.add(CategoryBudget(category_id=food.id, month="2026-03", allocated_cents=30000))
        s.commit()
    engine.dispose()
    return url


def test_insights_page_renders_new_sections(seeded_db: str) -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        str(REPO_ROOT / "app_pages/insights.py"), default_timeout=30
    )
    at.run()
    assert not at.exception, f"insights page raised: {at.exception}"


def test_insights_page_renders_with_empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Insights page must not crash against a fresh empty DB (no transactions, no budgets)."""
    db = tmp_path / "empty_insights.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    from local_ledger.db import run_migrations

    run_migrations(url)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(
        str(REPO_ROOT / "app_pages/insights.py"), default_timeout=30
    )
    at.run()
    assert not at.exception, f"insights page (empty DB) raised: {at.exception}"
