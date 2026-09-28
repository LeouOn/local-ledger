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
from sqlalchemy import create_engine, inspect, text

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


# --- TAX-01: import provenance columns and imported-id uniqueness ---


def test_staged_provenance_columns_present(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    eng = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(eng).get_columns("stagedtransaction")}
    finally:
        eng.dispose()
    assert {
        "notes",
        "source_row",
        "committed_transaction_id",
        "committed_at",
        "committed_by",
    } <= columns


def test_committed_transaction_foreign_key_present(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    eng = create_engine(url)
    try:
        fks = inspect(eng).get_foreign_keys("stagedtransaction")
    finally:
        eng.dispose()
    targets = {(fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
    assert ("transaction", ("id",)) in targets


def test_imported_id_unique_index_created(tmp_path: Path) -> None:
    """One source row id may produce at most one transaction per account."""
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")
    eng = create_engine(url)
    try:
        indexes = {ix["name"]: ix for ix in inspect(eng).get_indexes("transaction")}
    finally:
        eng.dispose()
    index = indexes.get("uq_transaction_account_imported_id")
    assert index is not None
    assert index["unique"]
    assert index["column_names"] == ["account_id", "imported_id"]


def test_upgrade_survives_pre_existing_duplicates(tmp_path: Path) -> None:
    """A workspace that already holds duplicates keeps its data and still upgrades.

    The index is skipped rather than the upgrade failing or transactions being
    deleted; ``ImportService.commit`` refuses the duplicate regardless.
    """
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "c3f8a1e62b94")
    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO account (name, type, currency, created_at) "
                    "VALUES ('a','checking','USD', '2026-01-01 00:00:00')"
                )
            )
            for _ in range(2):
                conn.execute(
                    text(
                        "INSERT INTO \"transaction\" "
                        "(account_id, date, amount_cents, imported_id, created_at, is_reconciled, "
                        "created_by) VALUES (1, '2026-01-10', -1500, 'FIT-1', "
                        "'2026-01-01 00:00:00', 0, 'import:old')"
                    )
                )
    finally:
        eng.dispose()

    command.upgrade(cfg, "head")  # must not raise

    eng = create_engine(url)
    try:
        indexes = {ix["name"] for ix in inspect(eng).get_indexes("transaction")}
        with eng.connect() as conn:
            rows = conn.execute(text('SELECT COUNT(*) FROM "transaction"')).scalar()
    finally:
        eng.dispose()
    assert "uq_transaction_account_imported_id" not in indexes
    assert rows == 2, "existing transactions must not be deleted"


# --- TAX-04: tax workspace and evidence lifecycle tables ---

TAX_TABLES = {
    "taxworkspace",
    "taxdocument",
    "extractedfield",
    "taxproperty",
    "rentalexpenserecord",
    "assetdepreciation",
    "investmentsalerecord",
    "jurisdictioncarryover",
    "taxquestion",
    "reconciliationitem",
    "evidencepacket",
}


def test_tax_tables_created_on_upgrade(tmp_path: Path) -> None:
    """All 11 tax domain tables are created upon upgrading to head."""
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    command.upgrade(_alembic_config(url), "head")

    tables = _table_names(db)
    missing = TAX_TABLES - tables
    assert not missing, f"migration did not create tax tables: {missing}"


def test_tax_tables_round_trip_downgrade(tmp_path: Path) -> None:
    """Tax tables are dropped when downgrading to e5f7a1c9d204 and restored on head."""
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")

    tables_head = _table_names(db)
    assert tables_head >= TAX_TABLES

    command.downgrade(cfg, "e5f7a1c9d204")
    tables_down = _table_names(db)
    assert not (TAX_TABLES & tables_down), f"downgrade left tax tables: {TAX_TABLES & tables_down}"
    assert tables_down >= EXPECTED_TABLES, "base budgeting tables must remain after downgrading tax revision"

    command.upgrade(cfg, "head")
    tables_reup = _table_names(db)
    assert tables_reup >= TAX_TABLES


def test_tax_upgrade_preserves_budgeting_data(tmp_path: Path) -> None:
    """Existing transactions and accounts remain completely intact when migrating to tax tables."""
    db = tmp_path / "ledger.db"
    url = f"sqlite:///{db.as_posix()}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "e5f7a1c9d204")

    eng = create_engine(url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO account (name, type, currency, created_at) "
                    "VALUES ('Checking','checking','USD', '2026-01-01 00:00:00')"
                )
            )
            conn.execute(
                text(
                    'INSERT INTO "transaction" '
                    "(account_id, date, amount_cents, imported_id, created_at, is_reconciled, "
                    "created_by) VALUES (1, '2026-02-15', 50000, 'TX-100', "
                    "'2026-02-15 12:00:00', 1, 'user')"
                )
            )
    finally:
        eng.dispose()

    # Upgrade to f6a8b2c4d305 (head)
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            tx_row = conn.execute(
                text('SELECT account_id, date, amount_cents, imported_id, is_reconciled FROM "transaction" WHERE id = 1')
            ).fetchone()
            acc_row = conn.execute(
                text("SELECT name, type, currency FROM account WHERE id = 1")
            ).fetchone()
    finally:
        eng.dispose()

    assert tx_row == (1, "2026-02-15", 50000, "TX-100", 1)
    assert acc_row == ("Checking", "checking", "USD")
