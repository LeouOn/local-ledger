"""Sanity tests proving the test infrastructure itself is wired correctly."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, text

from tests import conftest as root_conftest
from tests import factories


def test_tmp_engine_connects(engine: Engine) -> None:
    """The shared engine fixture yields a usable SQLite engine."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
    assert result == 1


def test_session_fixture_bound(session: Session) -> None:
    """The session fixture is bound to the engine and can run SQL."""
    value = session.exec(text("SELECT 42")).first()
    assert value == (42,)


def test_cassette_dir_resolvable(cassette_dir: Path) -> None:
    """VCR cassette directory is under tests/cassettes and is creatable."""
    assert cassette_dir == root_conftest.CASSETTE_DIR
    assert cassette_dir.exists()


def test_factories_module_importable() -> None:
    """The factories module imports (model factories land in todo 3)."""
    assert factories is not None
