"""Migration coverage for the TAX-06 hardening schema.

The interesting part is not "does the table exist" but what happens to
pre-hardening rows. A legacy classification with a single live row is a complete
decision and can be adopted. A legacy group with several live rows cannot be
shown to be a complete allocation, so it is moved to NEEDS_FACT and must be
re-reviewed - never silently approved, never silently deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

HARDENED_HEAD = "e2b47a9c6d10"
PRE_HARDENING_HEAD = "c3d91f6a2b48"


def _config(db_url: str) -> Config:
    os.environ["LOCAL_LEDGER_DB_URL"] = db_url
    cfg = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _upgrade_to(tmp_path: Path, revision: str) -> str:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    command.upgrade(_config(url), revision)
    return url


def _seed_legacy(url: str) -> None:
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO taxworkspace (id, tax_year, jurisdiction, residency_status, "
                "filing_status, status, created_at, updated_at) VALUES "
                "('ws-1', 2025, 'US-FED', 'UNKNOWN', 'UNKNOWN', 'DRAFT', "
                "'2026-01-01', '2026-01-01')"
            )
        )
        # one live row -> a complete allocation, adoptable
        c.execute(
            sa.text(
                "INSERT INTO taxclassification (id, workspace_id, subject_type, subject_id, "
                "treatment, state, amount_cents, allocation_bp, fact, created_at) VALUES "
                "('cls-a', 'ws-1', 'RENTAL_EXPENSE', 'exp-1', 'RENTAL_REPAIRS', 'CLASSIFIED', "
                "12345, 10000, 'single row', '2026-01-01')"
            )
        )
        # two live rows -> ambiguous, cannot be shown complete
        for rid, bp in (("cls-b", 5000), ("cls-c", 4000)):
            c.execute(
                sa.text(
                    "INSERT INTO taxclassification (id, workspace_id, subject_type, subject_id, "
                    "treatment, state, amount_cents, allocation_bp, fact, created_at) VALUES "
                    "(:id, 'ws-1', 'RENTAL_EXPENSE', 'exp-2', 'RENTAL_REPAIRS', 'CLASSIFIED', "
                    "9999, :bp, 'half', '2026-01-01')"
                ),
                {"id": rid, "bp": bp},
            )
    eng.dispose()


def test_new_tables_and_columns_exist(tmp_path: Path) -> None:
    url = _upgrade_to(tmp_path, "head")
    eng = sa.create_engine(url)
    try:
        inspector = sa.inspect(eng)
        tables = set(inspector.get_table_names())
        assert {"reviewscope", "decisionhead"} <= tables

        cols = {c["name"] for c in inspector.get_columns("taxclassification")}
        assert {
            "decision_id",
            "revision",
            "measure",
            "source_amount_cents",
            "source_fingerprint",
        } <= cols

        run_cols = {c["name"] for c in inspector.get_columns("reconciliationrun")}
        assert {"scope_version", "scope_snapshot_json", "complete", "incompleteness_reason"} <= run_cols
    finally:
        eng.dispose()


def test_decision_head_is_unique_per_subject_and_measure(tmp_path: Path) -> None:
    """The constraint is the concurrency guard, so it must exist in the schema."""
    url = _upgrade_to(tmp_path, "head")
    eng = sa.create_engine(url)
    try:
        constraints = {
            c["name"]
            for c in sa.inspect(eng).get_unique_constraints("decisionhead")
        }
        assert "uq_tax_decision_head" in constraints
    finally:
        eng.dispose()


def test_legacy_single_row_becomes_a_complete_revision(tmp_path: Path) -> None:
    url = _upgrade_to(tmp_path, PRE_HARDENING_HEAD)
    _seed_legacy(url)
    command.upgrade(_config(url), HARDENED_HEAD)

    eng = sa.create_engine(url)
    try:
        with eng.connect() as c:
            head = c.execute(
                sa.text(
                    "SELECT current_revision, version, measure FROM decisionhead "
                    "WHERE subject_id = 'exp-1'"
                )
            ).one()
            assert head == (1, 1, "AMOUNT")
            row = c.execute(
                sa.text(
                    "SELECT state, decision_id, revision, source_amount_cents "
                    "FROM taxclassification WHERE id = 'cls-a'"
                )
            ).one()
            assert row[0] == "CLASSIFIED"
            assert row[1] is not None and row[2] == 1
            assert row[3] == 12345
    finally:
        eng.dispose()


def test_legacy_ambiguous_group_is_never_silently_approved(tmp_path: Path) -> None:
    url = _upgrade_to(tmp_path, PRE_HARDENING_HEAD)
    _seed_legacy(url)
    command.upgrade(_config(url), HARDENED_HEAD)

    eng = sa.create_engine(url)
    try:
        with eng.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT id, state, decision_id, fact FROM taxclassification "
                    "WHERE subject_id = 'exp-2' ORDER BY id"
                )
            ).all()
            assert len(rows) == 2
            for _id, state, decision_id, fact in rows:
                assert state == "NEEDS_FACT"
                assert decision_id is None, "an ambiguous group gets no current decision"
                assert "re-review" in fact.lower()
            # No head was created for the ambiguous subject.
            heads = c.execute(
                sa.text("SELECT COUNT(*) FROM decisionhead WHERE subject_id = 'exp-2'")
            ).scalar()
            assert heads == 0
    finally:
        eng.dispose()


def test_legacy_rows_are_preserved_not_deleted(tmp_path: Path) -> None:
    url = _upgrade_to(tmp_path, PRE_HARDENING_HEAD)
    _seed_legacy(url)
    command.upgrade(_config(url), HARDENED_HEAD)
    eng = sa.create_engine(url)
    try:
        with eng.connect() as c:
            total = c.execute(sa.text("SELECT COUNT(*) FROM taxclassification")).scalar()
        assert total == 3
    finally:
        eng.dispose()


def test_hardened_migration_round_trips(tmp_path: Path) -> None:
    url = _upgrade_to(tmp_path, "head")
    command.downgrade(_config(url), PRE_HARDENING_HEAD)
    eng = sa.create_engine(url)
    try:
        tables = set(sa.inspect(eng).get_table_names())
        assert "reviewscope" not in tables
        assert "decisionhead" not in tables
        cols = {c["name"] for c in sa.inspect(eng).get_columns("taxclassification")}
        assert "decision_id" not in cols
    finally:
        eng.dispose()


def test_scope_starts_unconfigured_for_existing_workspaces(tmp_path: Path) -> None:
    """A workspace that existed before the change must not silently claim a scope."""
    url = _upgrade_to(tmp_path, PRE_HARDENING_HEAD)
    _seed_legacy(url)
    command.upgrade(_config(url), "head")

    from sqlmodel import Session

    from local_ledger.tax.scope import WorkspaceScopeService

    eng = sa.create_engine(url)
    try:
        with Session(eng) as s:
            scope = WorkspaceScopeService(s).get("ws-1")
            assert scope.mode == "UNCONFIGURED"
            assert scope.is_configured is False
    finally:
        eng.dispose()
