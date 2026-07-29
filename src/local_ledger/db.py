"""Database engine construction and schema bootstrap.

Two paths to a ready schema:
- ``init_db(engine)`` runs ``SQLModel.metadata.create_all`` - used by tests and
  the in-memory engine fixture.
- ``run_migrations(url)`` runs ``alembic upgrade head`` - used by the app's
  file-backed SQLite DB at startup (todo 3 wires the env; todo 20 calls it).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from local_ledger import models  # noqa: F401  - register tables on metadata

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "ledger.db"
DEFAULT_DB_URL = f"sqlite:///{DB_PATH.as_posix()}"


def init_db(engine: Engine) -> None:
    """Create all tables on ``engine`` (test/in-memory path)."""
    SQLModel.metadata.create_all(engine)


def run_migrations(db_url: str | None = None) -> None:
    """Run ``alembic upgrade head`` against ``db_url`` (app file-DB path).

    The URL is injected into the alembic config so migrations apply to whatever
    database the app is configured to use.
    """
    import os

    url = db_url or DEFAULT_DB_URL
    os.environ["LOCAL_LEDGER_DB_URL"] = url

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def ensure_data_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
