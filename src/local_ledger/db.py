"""Database engine construction and schema bootstrap.

Two paths to a ready schema:
- ``init_db(engine)`` runs ``SQLModel.metadata.create_all`` - used by tests and
  the in-memory engine fixture.
- ``run_migrations(url)`` runs ``alembic upgrade head`` - used by the app's
  file-backed SQLite DB at startup (todo 3 wires the env; todo 20 calls it).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import SQLModel

from local_ledger import models  # noqa: F401  - register tables on metadata

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "ledger.db"
DEFAULT_DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

T = TypeVar("T")

_LOCKED = "database is locked"


def enable_wal(engine: Engine) -> None:
    """Set WAL journal mode + busy_timeout on every new connection.

    No-op for in-memory engines (SQLite reports ``memory``); the busy_timeout
    pragma still applies and is harmless there.
    """

    @sa_event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=200")
        cur.close()


def transaction_with_retry(
    op: Callable[[], T],
    retries: int = 3,
    delays: tuple[float, ...] = (0.05, 0.1, 0.2),
) -> T:
    """Run ``op``; on a SQLite ``database is locked`` error, back off and retry.

    Raises the last error if all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return op()
        except OperationalError as exc:
            last_exc = exc
            if _LOCKED not in str(exc).lower():
                raise
            if attempt < retries - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


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
