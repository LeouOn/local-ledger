"""add tax workspace and evidence tables

Revision ID: f6a8b2c4d305
Revises: e5f7a1c9d204
Create Date: 2026-09-28

TAX-04: Persist tax workspace and evidence lifecycle.
Creates 11 SQLModel domain tables for annual tax preparation:
- taxworkspace
- taxdocument
- extractedfield
- taxproperty
- rentalexpenserecord
- assetdepreciation
- investmentsalerecord
- jurisdictioncarryover
- taxquestion
- reconciliationitem
- evidencepacket
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a8b2c4d305"
down_revision: str | Sequence[str] | None = "e5f7a1c9d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. TaxWorkspace
    op.create_table(
        "taxworkspace",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("jurisdiction", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("residency_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("filing_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tax_year", "jurisdiction", name="uq_tax_workspace_year_jurisdiction"),
    )
    op.create_index(op.f("ix_taxworkspace_tax_year"), "taxworkspace", ["tax_year"], unique=False)
    op.create_index(op.f("ix_taxworkspace_jurisdiction"), "taxworkspace", ["jurisdiction"], unique=False)
    op.create_index(op.f("ix_taxworkspace_status"), "taxworkspace", ["status"], unique=False)

    # 2. TaxDocument
    op.create_table(
        "taxdocument",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("file_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("sha256_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("doc_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("scan_state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_corrected", sa.Boolean(), nullable=False),
        sa.Column("supersedes_doc_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("storage_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.ForeignKeyConstraint(["supersedes_doc_id"], ["taxdocument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_taxdocument_workspace_id"), "taxdocument", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_taxdocument_sha256_hash"), "taxdocument", ["sha256_hash"], unique=False)
    op.create_index(op.f("ix_taxdocument_doc_type"), "taxdocument", ["doc_type"], unique=False)
    op.create_index(op.f("ix_taxdocument_scan_state"), "taxdocument", ["scan_state"], unique=False)

    # 3. ExtractedField
    op.create_table(
        "extractedfield",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("document_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("box_or_line", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("snippet_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("bounding_box_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("field_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("raw_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model_provenance_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("verified_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["taxdocument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_extractedfield_workspace_id"), "extractedfield", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_extractedfield_document_id"), "extractedfield", ["document_id"], unique=False)
    op.create_index(op.f("ix_extractedfield_field_name"), "extractedfield", ["field_name"], unique=False)
    op.create_index(op.f("ix_extractedfield_status"), "extractedfield", ["status"], unique=False)

    # 4. TaxProperty
    op.create_table(
        "taxproperty",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("street_address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("city", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("zip_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("ownership_basis_points", sa.Integer(), nullable=False),
        sa.Column("days_rented", sa.Integer(), nullable=False),
        sa.Column("days_personal_use", sa.Integer(), nullable=False),
        sa.Column("date_placed_in_service", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_taxproperty_workspace_id"), "taxproperty", ["workspace_id"], unique=False)

    # 5. RentalExpenseRecord
    op.create_table(
        "rentalexpenserecord",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("category", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("raw_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_document_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("box_or_line", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("treatment_authority", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_capital_improvement_suspect", sa.Boolean(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.ForeignKeyConstraint(["property_id"], ["taxproperty.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["taxdocument.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["transaction.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rentalexpenserecord_workspace_id"), "rentalexpenserecord", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_rentalexpenserecord_property_id"), "rentalexpenserecord", ["property_id"], unique=False)
    op.create_index(op.f("ix_rentalexpenserecord_category"), "rentalexpenserecord", ["category"], unique=False)
    op.create_index(op.f("ix_rentalexpenserecord_status"), "rentalexpenserecord", ["status"], unique=False)

    # 6. AssetDepreciation
    op.create_table(
        "assetdepreciation",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("date_placed_in_service", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cost_basis_cents", sa.Integer(), nullable=False),
        sa.Column("land_value_cents", sa.Integer(), nullable=False),
        sa.Column("depreciable_basis_cents", sa.Integer(), nullable=False),
        sa.Column("recovery_period_months", sa.Integer(), nullable=False),
        sa.Column("prior_depreciation_fed_cents", sa.Integer(), nullable=False),
        sa.Column("prior_depreciation_ca_cents", sa.Integer(), nullable=False),
        sa.Column("current_depreciation_fed_cents", sa.Integer(), nullable=False),
        sa.Column("current_depreciation_ca_cents", sa.Integer(), nullable=False),
        sa.Column("state_difference_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.ForeignKeyConstraint(["property_id"], ["taxproperty.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_assetdepreciation_workspace_id"), "assetdepreciation", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_assetdepreciation_property_id"), "assetdepreciation", ["property_id"], unique=False)

    # 7. InvestmentSaleRecord
    op.create_table(
        "investmentsalerecord",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_document_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("security_description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cusip_or_ticker", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("date_acquired", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("date_sold", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("proceeds_cents", sa.Integer(), nullable=False),
        sa.Column("cost_basis_cents", sa.Integer(), nullable=True),
        sa.Column("gain_loss_cents", sa.Integer(), nullable=True),
        sa.Column("term", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("basis_reported_to_irs", sa.Boolean(), nullable=False),
        sa.Column("wash_sale_disallowed_cents", sa.Integer(), nullable=False),
        sa.Column("fed_ca_difference_cents", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("raw_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["taxdocument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_investmentsalerecord_workspace_id"), "investmentsalerecord", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_investmentsalerecord_source_document_id"), "investmentsalerecord", ["source_document_id"], unique=False)
    op.create_index(op.f("ix_investmentsalerecord_status"), "investmentsalerecord", ["status"], unique=False)

    # 8. JurisdictionCarryover
    op.create_table(
        "jurisdictioncarryover",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("jurisdiction", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("category", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("property_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("prior_year_cents", sa.Integer(), nullable=True),
        sa.Column("utilized_current_year_cents", sa.Integer(), nullable=False),
        sa.Column("remaining_carryover_cents", sa.Integer(), nullable=True),
        sa.Column("prior_return_reference", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jurisdictioncarryover_workspace_id"), "jurisdictioncarryover", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_jurisdictioncarryover_jurisdiction"), "jurisdictioncarryover", ["jurisdiction"], unique=False)
    op.create_index(op.f("ix_jurisdictioncarryover_category"), "jurisdictioncarryover", ["category"], unique=False)

    # 9. TaxQuestion
    op.create_table(
        "taxquestion",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("question_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("context", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resolution", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("authority_citation", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_taxquestion_workspace_id"), "taxquestion", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_taxquestion_status"), "taxquestion", ["status"], unique=False)

    # 10. ReconciliationItem
    op.create_table(
        "reconciliationitem",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tax_section", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("field_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_working_paper_cents", sa.Integer(), nullable=False),
        sa.Column("taxact_entered_cents", sa.Integer(), nullable=False),
        sa.Column("delta_cents", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("explanation", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("authority_citation", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("consumed_transaction_ids_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reconciliationitem_workspace_id"), "reconciliationitem", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_reconciliationitem_field_key"), "reconciliationitem", ["field_key"], unique=False)
    op.create_index(op.f("ix_reconciliationitem_status"), "reconciliationitem", ["status"], unique=False)

    # 11. EvidencePacket
    op.create_table(
        "evidencepacket",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_draft", sa.Boolean(), nullable=False),
        sa.Column("is_stale", sa.Boolean(), nullable=False),
        sa.Column("snapshot_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["taxworkspace.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_evidencepacket_workspace_id"), "evidencepacket", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_evidencepacket_content_hash"), "evidencepacket", ["content_hash"], unique=False)


def downgrade() -> None:
    op.drop_table("evidencepacket")
    op.drop_table("reconciliationitem")
    op.drop_table("taxquestion")
    op.drop_table("jurisdictioncarryover")
    op.drop_table("investmentsalerecord")
    op.drop_table("assetdepreciation")
    op.drop_table("rentalexpenserecord")
    op.drop_table("taxproperty")
    op.drop_table("extractedfield")
    op.drop_table("taxdocument")
    op.drop_table("taxworkspace")
