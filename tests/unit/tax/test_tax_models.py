"""Unit tests for Tax SQLModel domain tables (TAX-04).

Validates integer cents constraints, ISO date formats, ID generation,
and enum values across the 11 tax domain models.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from local_ledger.tax.models import (
    AssetDepreciation,
    DocumentType,
    EvidencePacket,
    EvidenceStatus,
    ExtractedField,
    FilingStatus,
    HoldingTerm,
    InvestmentSaleRecord,
    Jurisdiction,
    JurisdictionCarryover,
    ReconciliationItem,
    ReconciliationStatus,
    RentalExpenseRecord,
    ResidencyStatus,
    ScanState,
    TaxDocument,
    TaxProperty,
    TaxQuestion,
    TaxWorkspace,
    WorkspaceStatus,
)


def test_tax_workspace_defaults_and_id_prefix() -> None:
    ws = TaxWorkspace(tax_year=2025)
    assert ws.id.startswith("ws-")
    assert ws.tax_year == 2025
    assert ws.jurisdiction == Jurisdiction.FEDERAL
    # Neither fact is confirmed by the user, so the default must not assert one.
    assert ws.residency_status == ResidencyStatus.UNKNOWN
    assert ws.filing_status == FilingStatus.UNKNOWN
    assert ws.status == WorkspaceStatus.DRAFT


def test_extracted_field_int_cents_validation() -> None:
    # Valid int cents
    field = ExtractedField(
        workspace_id="ws-123",
        document_id="doc-123",
        field_name="1099_DIV.box1a_ordinary_dividends",
        raw_text="$1,500.00",
        amount_cents=150000,
    )
    assert field.id.startswith("fld-")
    assert field.amount_cents == 150000
    assert field.status == EvidenceStatus.PROPOSED

    # Float must raise ValueError
    with pytest.raises(ValidationError, match="floats are forbidden"):
        ExtractedField(
            workspace_id="ws-123",
            document_id="doc-123",
            field_name="box1a",
            raw_text="12.34",
            amount_cents=12.34,  # type: ignore[arg-type]
        )

    # Boolean must raise ValueError
    with pytest.raises(ValidationError, match="not bool"):
        ExtractedField(
            workspace_id="ws-123",
            document_id="doc-123",
            field_name="box1a",
            raw_text="1",
            amount_cents=True,  # type: ignore[arg-type]
        )


def test_tax_property_and_rental_expense_models() -> None:
    prop = TaxProperty(
        workspace_id="ws-123",
        street_address="123 Oak St",
        city="San Jose",
        state="CA",
        zip_code="95112",
        date_placed_in_service="2021-06-01",
    )
    assert prop.id.startswith("prop-")
    assert prop.ownership_basis_points == 10000

    # Invalid ISO date placed in service
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        TaxProperty(
            workspace_id="ws-123",
            street_address="123 Oak St",
            city="San Jose",
            state="CA",
            zip_code="95112",
            date_placed_in_service="06/01/2021",
        )

    exp = RentalExpenseRecord(
        workspace_id="ws-123",
        property_id=prop.id,
        category="Repairs",
        amount_cents=45000,
        raw_text="$450.00",
    )
    assert exp.id.startswith("exp-")
    assert exp.amount_cents == 45000
    assert exp.status == EvidenceStatus.PROPOSED


def test_asset_depreciation_model() -> None:
    asset = AssetDepreciation(
        workspace_id="ws-123",
        property_id="prop-123",
        description="HVAC System Replacement",
        date_placed_in_service="2025-04-15",
        cost_basis_cents=850000,
        land_value_cents=0,
        depreciable_basis_cents=850000,
        recovery_period_months=330,
        current_depreciation_fed_cents=30909,
        current_depreciation_ca_cents=30909,
    )
    assert asset.id.startswith("ast-")
    assert asset.cost_basis_cents == 850000
    assert asset.recovery_period_months == 330
    assert asset.recovery_years == 27.5

    with pytest.raises(ValidationError, match="floats are forbidden"):
        AssetDepreciation(
            workspace_id="ws-123",
            property_id="prop-123",
            description="HVAC",
            date_placed_in_service="2025-04-15",
            cost_basis_cents=8500.50,  # type: ignore[arg-type]
            land_value_cents=0,
            depreciable_basis_cents=850050,
        )


def test_investment_sale_record_model() -> None:
    # Fully known basis
    sale = InvestmentSaleRecord(
        workspace_id="ws-123",
        source_document_id="doc-123",
        security_description="100 SH ACME CORP",
        date_sold="2025-11-20",
        date_acquired="2024-03-10",
        proceeds_cents=1000000,
        cost_basis_cents=800000,
        gain_loss_cents=200000,
        term=HoldingTerm.LONG_TERM,
    )
    assert sale.id.startswith("sec-")
    assert sale.gain_loss_cents == 200000

    # Missing / noncovered basis explicitly allowed as None
    sale_missing_basis = InvestmentSaleRecord(
        workspace_id="ws-123",
        source_document_id="doc-123",
        security_description="CRYPTO ASSET LOT 1",
        date_sold="2025-10-15",
        proceeds_cents=500000,
        cost_basis_cents=None,
        gain_loss_cents=None,
        basis_reported_to_irs=False,
    )
    assert sale_missing_basis.cost_basis_cents is None
    assert sale_missing_basis.gain_loss_cents is None

    # Invalid sold date
    with pytest.raises(ValidationError, match="YYYY-MM-DD"):
        InvestmentSaleRecord(
            workspace_id="ws-123",
            source_document_id="doc-123",
            security_description="ACME",
            date_sold="not-a-date",
            proceeds_cents=100,
            cost_basis_cents=50,
            gain_loss_cents=50,
        )


def test_jurisdiction_carryover_and_reconciliation_item() -> None:
    # Known carryover
    carryover = JurisdictionCarryover(
        workspace_id="ws-123",
        jurisdiction=Jurisdiction.CALIFORNIA,
        category="CAPITAL_LOSS",
        prior_year_cents=300000,
        utilized_current_year_cents=300000,
        remaining_carryover_cents=0,
    )
    assert carryover.id.startswith("cry-")
    assert carryover.prior_year_cents == 300000

    # Unknown carryover when 2024 return is not available
    unknown_carryover = JurisdictionCarryover(
        workspace_id="ws-123",
        jurisdiction=Jurisdiction.CALIFORNIA,
        category="PASSIVE_LOSS",
        prior_year_cents=None,
        remaining_carryover_cents=None,
    )
    assert unknown_carryover.prior_year_cents is None

    rec = ReconciliationItem(
        workspace_id="ws-123",
        tax_section="Schedule D",
        field_key="net_long_term_gain",
        source_working_paper_cents=250000,
        taxact_entered_cents=250000,
        delta_cents=0,
        status=ReconciliationStatus.MATCHED,
    )
    assert rec.id.startswith("rec-")
    assert rec.status == ReconciliationStatus.MATCHED


def test_tax_document_and_evidence_packet() -> None:
    doc = TaxDocument(
        workspace_id="ws-123",
        file_name="1099-B.pdf",
        sha256_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        doc_type=DocumentType.FORM_1099_B,
        scan_state=ScanState.TEXT_EXTRACTABLE,
    )
    assert doc.id.startswith("doc-")
    assert doc.is_corrected is False

    pkt = EvidencePacket(
        workspace_id="ws-123",
        content_hash="abc123hash",
        is_draft=True,
    )
    assert pkt.id.startswith("pkt-")
    assert pkt.is_stale is False

    question = TaxQuestion(
        workspace_id="ws-123",
        question_text="Is the roof repair deductible or capital?",
    )
    assert question.id.startswith("qst-")
    assert question.status == "OPEN"
