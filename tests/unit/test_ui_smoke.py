"""AppTest smoke tests: each page boots against a temp DB without exception.

These exercise the real Streamlit surface headlessly. The DB is redirected to a
temp file via LOCAL_LEDGER_DB_URL. Ollama is absent in CI, so pages must degrade
gracefully (banner) rather than crash.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LOCAL_LEDGER_SKIP_UI_SMOKE") == "1",
    reason="UI smoke skipped by env",
)


@pytest.fixture()
def temp_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "smoke.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)
    # Ollama is absent under the unit socket guard; simulate NOT_RUNNING so pages
    # render the degradation banner instead of probing a real socket.
    from local_ledger.llm.ollama import OllamaState

    monkeypatch.setattr(
        "local_ledger.ui.chrome.detect_state", lambda *a, **k: OllamaState.NOT_RUNNING
    )
    return url


def _run_page(page_module: str) -> None:
    """Render a page in AppTest, asserting no exception."""
    from streamlit.testing.v1 import AppTest

    repo_root = Path(__file__).resolve().parent.parent.parent
    at = AppTest.from_file(str(repo_root / page_module), default_timeout=30)
    at.run()
    assert not at.exception, f"{page_module} raised: {at.exception}"


def test_app_entry_boots(temp_db_url: str) -> None:
    _run_page("app.py")


def test_home_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/home.py")


def test_transactions_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/transactions.py")


def test_budgets_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/budgets.py")


def test_import_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/import_.py")


def test_insights_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/insights.py")


def test_settings_page_renders(temp_db_url: str) -> None:
    _run_page("app_pages/settings.py")
