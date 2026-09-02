"""Top-level pytest configuration shared by unit, integration, and e2e tests.

Provides an isolated SQLite engine + session fixture. Schema creation is
delegated to :mod:`local_ledger.db` (todo 3); tests that need tables call
``init_db(engine)`` (imported lazily so this module imports cleanly before the
models exist).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


class AppTestRunner:
    """Callable page-boot checker; exposes ``db_url`` for optional seeding."""

    def __init__(self, run: Callable[[str], None], db_url: str) -> None:
        self.run = run
        self.db_url = db_url

    def __call__(self, page: str) -> None:
        self.run(page)


@pytest.fixture()
def apptest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppTestRunner]:
    """Hermetic Streamlit page-boot check in a fresh subprocess.

    Streamlit leaks form/widget state across ``AppTest.run()`` calls within one
    process, which makes page-load tests order-dependent when run as a full
    suite. This fixture migrates a throwaway DB, then runs the page under
    AppTest in a child process with Ollama forced to NOT_RUNNING, failing if
    the page raises. Seed via ``runner.db_url`` before calling, e.g.::

        def test_x(apptest) -> None:
            seed(apptest.db_url)
            apptest("app_pages/home.py")
    """
    db = tmp_path / "apptest.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("LOCAL_LEDGER_DB_URL", url)

    from local_ledger.db import run_migrations

    run_migrations(url)

    def _run(page: str) -> None:
        runner = tmp_path / "_apptest_runner.py"
        runner.write_text(
            textwrap.dedent(
                f"""
                import sys
                import local_ledger.ui.chrome as chrome
                from local_ledger.llm.ollama import OllamaState
                chrome.detect_state = lambda *a, **k: OllamaState.NOT_RUNNING
                from streamlit.testing.v1 import AppTest
                at = AppTest.from_file(r"{REPO_ROOT / page}", default_timeout=30)
                at.run()
                if at.exception:
                    sys.stderr.write(str(at.exception))
                    sys.exit(1)
                """
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(runner)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "LOCAL_LEDGER_DB_URL": url},
        )
        assert result.returncode == 0, f"{page} raised:\n{result.stderr[-2000:]}"

    yield AppTestRunner(_run, url)


@pytest.fixture(autouse=True)
def _clear_streamlit_caches() -> Iterator[None]:
    """Reset Streamlit resource/data caches between tests.

    ``ui.chrome.get_session`` is ``@st.cache_resource``-decorated with no
    arguments, so without this fixture the first AppTest to touch it binds the
    cached session to that test's temp DB and every later AppTest silently
    reuses the stale session against the wrong database.
    """
    try:
        import streamlit as st

        st.cache_resource.clear()
        st.cache_data.clear()
    except Exception:
        pass
    yield
    try:
        st.cache_resource.clear()
        st.cache_data.clear()
    except Exception:
        pass


def _build_engine() -> Engine:
    """A single-connection in-memory SQLite engine with WAL-friendly settings.

    StaticPool keeps the single in-memory connection alive across the session so
    ``create_all`` + queries share state. ``check_same_thread=False`` lets the
    WAL concurrency stress test (todo 26) use threads against the same engine.
    """
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = _build_engine()
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """A SQLModel session bound to the isolated engine.

    Schema is *not* created here; tests that need tables should depend on the
    ``schema`` fixture (added in todo 3) or call ``init_db`` explicitly.
    """
    with Session(engine) as s:
        yield s


@pytest.fixture()
def cassette_dir() -> Path:
    """Guarantee the VCR cassette directory exists and return it."""
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    return CASSETTE_DIR
