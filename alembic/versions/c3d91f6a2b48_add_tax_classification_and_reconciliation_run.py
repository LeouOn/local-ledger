"""add tax classification and reconciliation run tables

Revision ID: c3d91f6a2b48
Revises: f6a8b2c4d305
Create Date: 2026-09-29

TAX-06 (reconciliation and classification service).

Adds the two concepts TAX-06 needs and TAX-04 deliberately did not model, because
they are a reviewed judgement rather than a fact read off a form:

- ``taxclassification``: a treatment for one subject (a ledger row, a rental
  expense line, an investment sale), the fact and authority supporting it, and
  the basis-point share that makes a split add up without double counting.
- ``reconciliationrun``: a computed snapshot of one basis (source-form or
  bank-flow) with a content hash over its totals *and their members*, so a
  figure can be taken apart to the evidence beneath it and a changed run is
  detectable without diffing every number.

The totals and their member ids are stored on the run as canonical JSON, matching
the existing ``consumed_transaction_ids_json`` convention in this schema, rather
than as two more tables that would only ever be read together.
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d91f6a2b48"
down_revision: str | Sequence[str] | None = "f6a8b2c4d305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "taxclassification",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("subject_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("subject_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("treatment", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("allocation_bp", sa.Integer(), nullable=False),
        sa.Column("fact", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authority_citation", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reviewed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_taxclassification_workspace_id"), "taxclassification", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_taxclassification_subject_type"), "taxclassification", ["subject_type"], unique=False)
    op.create_index(op.f("ix_taxclassification_subject_id"), "taxclassification", ["subject_id"], unique=False)
    op.create_index(op.f("ix_taxclassification_property_id"), "taxclassification", ["property_id"], unique=False)
    op.create_index(op.f("ix_taxclassification_treatment"), "taxclassification", ["treatment"], unique=False)
    op.create_index(op.f("ix_taxclassification_state"), "taxclassification", ["state"], unique=False)

    op.create_table(
        "reconciliationrun",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("basis", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("unresolved_cents", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.Column("totals_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reconciliationrun_workspace_id"), "reconciliationrun", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_reconciliationrun_tax_year"), "reconciliationrun", ["tax_year"], unique=False)
    op.create_index(op.f("ix_reconciliationrun_basis"), "reconciliationrun", ["basis"], unique=False)
    op.create_index(op.f("ix_reconciliationrun_content_hash"), "reconciliationrun", ["content_hash"], unique=False)
    op.create_index(op.f("ix_reconciliationrun_state"), "reconciliationrun", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reconciliationrun_state"), table_name="reconciliationrun")
    op.drop_index(op.f("ix_reconciliationrun_content_hash"), table_name="reconciliationrun")
    op.drop_index(op.f("ix_reconciliationrun_basis"), table_name="reconciliationrun")
    op.drop_index(op.f("ix_reconciliationrun_tax_year"), table_name="reconciliationrun")
    op.drop_index(op.f("ix_reconciliationrun_workspace_id"), table_name="reconciliationrun")
    op.drop_table("reconciliationrun")

    op.drop_index(op.f("ix_taxclassification_state"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_treatment"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_property_id"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_subject_id"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_subject_type"), table_name="taxclassification")
    op.drop_index(op.f("ix_taxclassification_workspace_id"), table_name="taxclassification")
    op.drop_table("taxclassification")
