"""Top-level pytest configuration shared by unit, integration, and e2e tests.

Provides an isolated SQLite engine + session fixture. Schema creation is
delegated to :mod:`local_ledger.db` (todo 3); tests that need tables call
``init_db(engine)`` (imported lazily so this module imports cleanly before the
models exist).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"


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
