"""Alembic migration round-trip tests.

Validates the generated initial migration applies cleanly to an empty file DB
and downgrades back to base, leaving the expected set of tables.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

EXPECTED_TABLES = {
    "account",
    "payee",
    "categorygroup",
    "category",
    "transaction",
    "categorybudget",
    "recommendation",
    "auditlog",
    "providersettings",
    "importprefs",
    "stagedtransaction",
}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_config(db_url: str) -> Config:
    os.environ["LOCAL_LEDGER_DB_URL"] = db_url
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _table_names(db_path: Path) -> set[str]:
    eng = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_alembic_upgrade_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")

    tables = _table_names(db)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"migration did not create: {missing}"
    assert "alembic_version" in tables


def test_alembic_downgrade_to_base_is_clean(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    command.downgrade(_alembic_config(url), "base")

    tables = _table_names(db)
    leftover = EXPECTED_TABLES & tables
    assert not leftover, f"downgrade left tables behind: {leftover}"


@pytest.mark.parametrize("table", sorted(EXPECTED_TABLES))
def test_transaction_check_constraint_present(tmp_path: Path, table: str) -> None:
    """The nonzero-amount CHECK survives into the migrated schema for transactions."""
    if table != "transaction":
        pytest.skip("check-constraint test only applies to transaction table")
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    eng = create_engine(url)
    try:
        constraints = inspect(eng).get_check_constraints("transaction")
    finally:
        eng.dispose()
    names = {c["name"] for c in constraints}
    assert "ck_transaction_amount_nonzero" in names
