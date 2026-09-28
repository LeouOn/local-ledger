"""add import provenance columns and transaction imported-id uniqueness

Revision ID: e5f7a1c9d204
Revises: c3f8a1e62b94
Create Date: 2026-09-28

TAX-01 (import integrity). Adds the staged-row provenance columns that make a
committed transaction traceable back to its source file and make a repeated
commit a no-op, plus a database-level guarantee that one source-system row id
produces at most one transaction per account.

The unique index is created only when existing rows already satisfy it. A
workspace that already contains duplicates from the pre-TAX-01 commit path
keeps its data; the migration logs and skips the index instead of failing the
upgrade or deleting transactions. ``ImportService.commit`` enforces the same
rule without the index, so correctness never depends on it.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  # needed for sqlmodel.sql.sqltypes below


logger = logging.getLogger("alembic.runtime.migration")

# revision identifiers, used by Alembic.
revision: str = "e5f7a1c9d204"
down_revision: Union[str, Sequence[str], None] = "c3f8a1e62b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_transaction_account_imported_id"


def _duplicate_imported_ids(conn) -> int:  # type: ignore[no-untyped-def]
    # "transaction" is a reserved word in SQLite; quote the table name.
    rows = conn.execute(
        sa.text(
            'SELECT account_id, imported_id, COUNT(*) FROM "transaction" '
            "WHERE imported_id IS NOT NULL "
            "GROUP BY account_id, imported_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    return len(rows)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("stagedtransaction", schema=None) as batch_op:
        batch_op.add_column(sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column("source_row", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("committed_transaction_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("committed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("committed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.create_foreign_key(
            "fk_stagedtransaction_committed_transaction_id",
            "transaction",
            ["committed_transaction_id"],
            ["id"],
        )

    conn = op.get_bind()
    duplicates = _duplicate_imported_ids(conn)
    if duplicates:
        logger.warning(
            "TAX-01: skipped unique index %s - %d (account_id, imported_id) group(s) already "
            "have duplicate transactions. ImportService.commit still refuses to write a second "
            "transaction for one source row id; resolve the existing duplicates to re-enable it.",
            _INDEX_NAME,
            duplicates,
        )
        return
    op.create_index(
        _INDEX_NAME,
        "transaction",
        ["account_id", "imported_id"],
        unique=True,
        sqlite_where=sa.text("imported_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_INDEX_NAME, table_name="transaction", sqlite_where=sa.text("imported_id IS NOT NULL"))
    with op.batch_alter_table("stagedtransaction", schema=None) as batch_op:
        batch_op.drop_constraint("fk_stagedtransaction_committed_transaction_id", type_="foreignkey")
        batch_op.drop_column("committed_by")
        batch_op.drop_column("committed_at")
        batch_op.drop_column("committed_transaction_id")
        batch_op.drop_column("source_row")
        batch_op.drop_column("notes")
