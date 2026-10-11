"""add review scope, decision heads and source-bound classification revisions

Revision ID: e2b47a9c6d10
Revises: c3d91f6a2b48
Create Date: 2026-09-29

TAX-06 hardening. Three additions, all serving one idea: a classification is a
reviewed statement about a specific source row, owned as a versioned decision,
inside a scope the workspace can prove.

- ``taxscope``: the workspace's review boundary (mode, accounts, properties, and
  a version), so a stored run can say what it covered. New workspaces start
  UNCONFIGURED, which cannot yield a complete run.
- ``decisionhead``: one head per (workspace, subject, measure) holding the
  current revision. The unique constraint is the concurrency guard; ``version`` is
  the compare-and-set token.
- ``taxclassification`` gains ``decision_id``, ``revision``, ``measure``,
  ``source_amount_cents`` and ``source_fingerprint``, so a decision records which
  revision it belongs to and which source amount was actually reviewed.

Legacy rows are preserved and never silently approved. A legacy group with
exactly one live row becomes a complete revision; any group that is ambiguous -
more than one live row, so its basis-point shares cannot be shown to total
10000 - is moved to NEEDS_FACT with a recorded reason, because an ambiguous
allocation must be re-reviewed rather than guessed at.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e2b47a9c6d10"
down_revision: str | Sequence[str] | None = "c3d91f6a2b48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AMBIGUOUS_REASON = (
    "pre-hardening classification: this subject had more than one live row, so its "
    "allocation could not be shown to be complete. Re-review the treatment."
)


def upgrade() -> None:
    # 1. New classification columns (nullable first, so existing rows survive).
    with op.batch_alter_table("taxclassification", schema=None) as batch_op:
        batch_op.add_column(sa.Column("decision_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column("revision", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("measure", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(sa.Column("source_amount_cents", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("source_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )

    # 2. Decision heads.
    op.create_table(
        "decisionhead",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("subject_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("subject_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("measure", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "subject_type",
            "subject_id",
            "measure",
            name="uq_tax_decision_head",
        ),
    )
    op.create_index(op.f("ix_decisionhead_workspace_id"), "decisionhead", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_decisionhead_subject_type"), "decisionhead", ["subject_type"], unique=False)
    op.create_index(op.f("ix_decisionhead_subject_id"), "decisionhead", ["subject_id"], unique=False)
    op.create_index(op.f("ix_decisionhead_measure"), "decisionhead", ["measure"], unique=False)
    op.create_index(op.f("ix_decisionhead_version"), "decisionhead", ["version"], unique=False)

    op.create_index(
        op.f("ix_taxclassification_decision_id"), "taxclassification", ["decision_id"], unique=False
    )
    op.create_index(op.f("ix_taxclassification_revision"), "taxclassification", ["revision"], unique=False)
    op.create_index(op.f("ix_taxclassification_measure"), "taxclassification", ["measure"], unique=False)
    op.create_index(
        op.f("ix_taxclassification_source_fingerprint"),
        "taxclassification",
        ["source_fingerprint"],
        unique=False,
    )

    # 3. A run records the scope it covered, so it can still explain itself later.
    with op.batch_alter_table("reconciliationrun", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope_version", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("scope_snapshot_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "complete", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch_op.add_column(
            sa.Column("incompleteness_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )

    # 4. Review scope, one row per workspace, starting UNCONFIGURED.
    op.create_table(
        "reviewscope",
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mode", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("account_ids_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_ids_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("properties_confirmed", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_index(op.f("ix_reviewscope_mode"), "reviewscope", ["mode"], unique=False)
    op.create_index(op.f("ix_reviewscope_version"), "reviewscope", ["version"], unique=False)

    _backfill_legacy_decisions(conn=op.get_bind())


def _backfill_legacy_decisions(conn) -> None:  # type: ignore[no-untyped-def]
    """Group pre-existing classifications into heads, conservatively."""
    rows = conn.execute(
        sa.text(
            "SELECT id, workspace_id, subject_type, subject_id, amount_cents "
            "FROM taxclassification "
            "WHERE state = 'CLASSIFIED' "
            "ORDER BY workspace_id, subject_type, subject_id, id"
        )
    ).fetchall()

    groups: dict[tuple[str, str, str], list[tuple]] = {}
    for row in rows:
        groups.setdefault((row[1], row[2], row[3]), []).append(row)

    for (workspace_id, subject_type, subject_id), members in groups.items():
        if len(members) == 1:
            head_id = f"hd-legacy-{members[0][0]}"
            conn.execute(
                sa.text(
                    "INSERT INTO decisionhead "
                    "(id, workspace_id, subject_type, subject_id, measure, current_revision, "
                    " version, updated_at) "
                    "VALUES (:id, :ws, :st, :si, 'AMOUNT', 1, 1, CURRENT_TIMESTAMP)"
                ),
                {"id": head_id, "ws": workspace_id, "st": subject_type, "si": subject_id},
            )
            conn.execute(
                sa.text(
                    "UPDATE taxclassification SET decision_id = :head, revision = 1, "
                    "measure = 'AMOUNT', source_amount_cents = amount_cents "
                    "WHERE id = :id"
                ),
                {"head": head_id, "id": members[0][0]},
            )
        else:
            # Ambiguous: cannot be shown to be a complete 10000bp allocation.
            for member in members:
                conn.execute(
                    sa.text(
                        "UPDATE taxclassification SET state = 'NEEDS_FACT', fact = :reason "
                        "WHERE id = :id"
                    ),
                    {"reason": _AMBIGUOUS_REASON, "id": member[0]},
                )

    # Every remaining live row predates decision ownership; give the unresolved
    # ones a measure so they are never read as a bare amount.
    conn.execute(
        sa.text("UPDATE taxclassification SET measure = 'AMOUNT' WHERE measure IS NULL")
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_taxclassification_source_fingerprint"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_measure"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_revision"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_decision_id"), table_name="taxclassification")

    op.drop_index(op.f("ix_reviewscope_version"), table_name="reviewscope")
    op.drop_index(op.f("ix_reviewscope_mode"), table_name="reviewscope")
    op.drop_table("reviewscope")

    op.drop_index(op.f("ix_decisionhead_version"), table_name="decisionhead")
    op.drop_index(op.f("ix_decisionhead_measure"), table_name="decisionhead")
    op.drop_index(op.f("ix_decisionhead_subject_id"), table_name="decisionhead")
    op.drop_index(op.f("ix_decisionhead_subject_type"), table_name="decisionhead")
    op.drop_index(op.f("ix_decisionhead_workspace_id"), table_name="decisionhead")
    op.drop_table("decisionhead")

    with op.batch_alter_table("reconciliationrun", schema=None) as batch_op:
        batch_op.drop_column("incompleteness_reason")
        batch_op.drop_column("complete")
        batch_op.drop_column("scope_snapshot_json")
        batch_op.drop_column("scope_version")

    with op.batch_alter_table("taxclassification", schema=None) as batch_op:
        batch_op.drop_column("source_fingerprint")
        batch_op.drop_column("source_amount_cents")
        batch_op.drop_column("measure")
        batch_op.drop_column("revision")
        batch_op.drop_column("decision_id")
