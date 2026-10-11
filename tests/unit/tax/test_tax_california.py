"""Unit tests for California Tax Differences, Sourcing, and Carryovers (TAX-15).

Validates:
1. Seeded official statutory authority registry (R&TC, FTB forms, conformity rules).
2. Residency status evaluation (FULL_YEAR, PART_YEAR, NON_RESIDENT, UNKNOWN):
   - UNKNOWN residency raises blocking questions and forbids verified status.
   - Property sourcing: California real property is 100% CA source; out-of-state property
     is 0% CA source for nonresidents, 100% worldwide for full-year residents.
   - Part-year resident flags need for residency dates / dual allocation (Form 540NR).
3. Separate federal vs. California depreciation and basis tracking (FTB 3885A vs Form 4562):
   - Bonus depreciation non-conformity (IRC § 168(k) vs R&TC § 17250).
   - Higher CA depreciation produces Schedule CA Column B subtraction.
   - Conforming 27.5-year straight line residential structure produces zero adjustment.
   - Differing remaining basis tracked upon disposition.
4. Federal vs. California prior-year carryovers:
   - Capital loss carryovers with annual $3,000 ordinary income offset limit.
   - Missing prior-year return (None) is NEVER silently coerced to $0; raises review gap.
   - Passive activity suspended rental losses (Form 8582 vs FTB 3801).
5. Schedule CA (540) adjustment compilation and TaxAct comparison generation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.tax import (
    AdjustmentType,
    CaliforniaTaxService,
    Jurisdiction,
    ReconciliationStatus,
    ResidencyStatus,
    TaxWorkspaceService,
)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def ca_service(session: Session, engine) -> CaliforniaTaxService:
    init_db(engine)
    return CaliforniaTaxService(session)


@pytest.fixture()
def tax_service(session: Session, engine) -> TaxWorkspaceService:
    init_db(engine)
    return TaxWorkspaceService(session)


# ---------------------------------------------------------------------------
# 1. Authority Registry Tests
# ---------------------------------------------------------------------------
def test_official_authority_registry_seeded(ca_service: CaliforniaTaxService) -> None:
    """Verify official California authorities are registered with statutory citations."""
    auths = ca_service.get_all_authorities()
    assert len(auths) >= 8

    # Verify key topics
    topics = {a.topic_id for a in auths}
    assert "BONUS_DEPRECIATION" in topics
    assert "CAPITAL_LOSS_CARRYOVER" in topics
    assert "PASSIVE_RENTAL_LOSS" in topics
    assert "RESIDENTIAL_RENTAL_DEPRECIATION" in topics
    assert "SECTION_179_EXPENSE" in topics
    assert "MUNICIPAL_BOND_INTEREST" in topics
    assert "HSA_NONCONFORMITY" in topics
    assert "US_OBLIGATION_INTEREST" in topics

    # Inspect bonus depreciation authority
    bonus_auth = ca_service.get_authority("BONUS_DEPRECIATION")
    assert bonus_auth is not None
    assert "168(k)" in bonus_auth.statutory_authority
    assert "FTB 3885A" in bonus_auth.ftb_form
    assert "Line 8f" in bonus_auth.schedule_ca_line

    # Inspect capital loss authority
    cap_auth = ca_service.get_authority("CAPITAL_LOSS_CARRYOVER")
    assert cap_auth is not None
    assert "18151" in cap_auth.statutory_authority
    assert "Schedule D" in cap_auth.ftb_form


# ---------------------------------------------------------------------------
# 2. Residency and Property Location Sourcing Tests
# ---------------------------------------------------------------------------
def test_unknown_residency_blocks_verification(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Unknown residency status raises blocking questions and forbids verified status."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.UNKNOWN,
    )

    analysis = ca_service.analyze_residency_and_sourcing(ws.id)
    assert analysis.is_determined is False
    assert analysis.can_verify_california is False
    assert len(analysis.blocking_questions) >= 1
    assert "UNKNOWN" in analysis.blocking_questions[0]

    # Schedule CA report reflects unresolved blocker
    report = ca_service.compute_schedule_ca_adjustments(ws.id)
    assert report.is_ready_for_review is False
    assert any("UNKNOWN" in q for q in report.unresolved_questions)


def test_full_year_resident_sourcing(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Full-year resident is taxed on worldwide income (both in-state and out-of-state properties)."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )

    # In-state California rental property
    prop_ca = tax_service.create_rental_property(
        ws.id, "123 Citrus Lane", "Riverside", "CA", "92501"
    )
    # Out-of-state rental property (e.g. Washington)
    prop_wa = tax_service.create_rental_property(
        ws.id, "456 Pine St", "Seattle", "WA", "98101"
    )

    analysis = ca_service.analyze_residency_and_sourcing(ws.id)
    assert analysis.is_determined is True
    assert analysis.can_verify_california is True
    assert "worldwide income" in analysis.sourcing_summary.lower()

    # Both properties allocated 100% to California tax base (worldwide taxation)
    assert analysis.property_sourcing[prop_ca.id]["allocation_percentage"] == 100
    assert analysis.property_sourcing[prop_ca.id]["is_california_property"] is True
    assert analysis.property_sourcing[prop_wa.id]["allocation_percentage"] == 100
    assert analysis.property_sourcing[prop_wa.id]["is_california_property"] is False


def test_nonresident_sourcing_excludes_out_of_state_property(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Nonresident is taxed ONLY on California sources; out-of-state property is 0% CA source."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.NON_RESIDENT,
    )

    prop_ca = tax_service.create_rental_property(
        ws.id, "100 Ocean Blvd", "San Diego", "CA", "92101"
    )
    prop_tx = tax_service.create_rental_property(
        ws.id, "200 Lone Star Way", "Austin", "TX", "78701"
    )

    analysis = ca_service.analyze_residency_and_sourcing(ws.id)
    assert analysis.is_determined is True
    assert analysis.can_verify_california is True

    # CA property is 100% CA source
    assert analysis.property_sourcing[prop_ca.id]["allocation_percentage"] == 100
    # Out-of-state TX property is 0% CA source
    assert analysis.property_sourcing[prop_tx.id]["allocation_percentage"] == 0
    assert "Excluded" in analysis.property_sourcing[prop_tx.id]["notes"]


def test_part_year_resident_flags_period_allocation(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Part-year resident requires dual-allocation and cannot be marked verified without dates."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.PART_YEAR,
    )

    analysis = ca_service.analyze_residency_and_sourcing(ws.id)
    assert analysis.is_determined is True
    assert analysis.can_verify_california is False
    assert any("540NR" in q or "allocation" in q.lower() for q in analysis.blocking_questions)


# ---------------------------------------------------------------------------
# 3. Depreciation Differences (FTB 3885A)
# ---------------------------------------------------------------------------
def test_bonus_depreciation_difference_subtraction(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Higher California depreciation in later recovery years creates Schedule CA Column B subtraction."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )
    prop = tax_service.create_rental_property(ws.id, "123 Main St", "Pasadena", "CA", "91101")

    # Appliance package from fixture:
    # Cost: $3,200 ($320,000 cents). In 2023, federal claimed 80% bonus.
    # 2025 Fed dep: $320 ($32,000 cents). 2025 CA dep: $640 ($64,000 cents).
    asset = ca_service.record_depreciable_asset(
        workspace_id=ws.id,
        property_id=prop.id,
        description="Appliance Package (Stove/Refrigerator)",
        date_placed_in_service="2023-01-15",
        cost_basis_cents=320000,
        land_value_cents=0,
        recovery_period_months=60,
        federal_prior_depreciation_cents=256000,
        california_prior_depreciation_cents=144000,
        federal_current_depreciation_cents=32000,
        california_current_depreciation_cents=64000,
        state_difference_reason="Bonus depreciation non-conformity (IRC § 168(k) vs FTB 3885A)",
    )
    assert asset.id.startswith("ast-")

    diffs = ca_service.analyze_depreciation_differences(ws.id)
    assert len(diffs) == 1
    d = diffs[0]

    # Delta: CA $640 - Fed $320 = +$320 (higher CA deduction)
    assert d.current_year_delta_cents == 32000
    assert d.schedule_ca_column == AdjustmentType.SUBTRACTION_COL_B
    assert d.adjustment_amount_cents == 32000

    # Remaining basis check:
    # Fed remaining: $3,200 - ($2,560 + $320) = $320 ($32,000 cents)
    # CA remaining: $3,200 - ($1,440 + $640) = $1,120 ($112,000 cents)
    assert d.federal_remaining_basis_cents == 32000
    assert d.california_remaining_basis_cents == 112000
    assert d.basis_difference_cents == 80000  # CA remaining basis is $800 higher


def test_conforming_residential_structure_depreciation(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Residential rental structure (27.5-year straight line) has zero difference under conformity."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )
    prop = tax_service.create_rental_property(ws.id, "456 Oak Ave", "Fresno", "CA", "93701")

    # Structure: $200,000 total, $40,000 land, $160,000 depreciable basis
    # Fed & CA depreciation are identical: $5,818 ($581,818 cents)
    ca_service.record_depreciable_asset(
        workspace_id=ws.id,
        property_id=prop.id,
        description="Residential Rental Building Structure",
        date_placed_in_service="2021-08-01",
        cost_basis_cents=20000000,
        land_value_cents=4000000,
        recovery_period_months=330,
        federal_prior_depreciation_cents=1939393,
        california_prior_depreciation_cents=1939393,
        federal_current_depreciation_cents=581818,
        california_current_depreciation_cents=581818,
        state_difference_reason="Residential Structure S/L 27.5-year (Conforming)",
    )

    diffs = ca_service.analyze_depreciation_differences(ws.id)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.current_year_delta_cents == 0
    assert d.schedule_ca_column == AdjustmentType.NO_DIFFERENCE
    assert d.adjustment_amount_cents == 0
    assert d.basis_difference_cents == 0


# ---------------------------------------------------------------------------
# 4. Prior-Year Carryover Tests (Schedule D & FTB 3801)
# ---------------------------------------------------------------------------
def test_capital_loss_carryover_tracking(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Capital loss carryover records federal vs CA with $3,000 annual limit."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )

    # Federal carryover: $7,500 prior, $3,000 utilized, $4,500 remaining
    ca_service.record_carryover(
        workspace_id=ws.id,
        jurisdiction=Jurisdiction.FEDERAL,
        category="CAPITAL_LOSS",
        prior_year_cents=750000,
        utilized_current_year_cents=300000,
        prior_return_reference="2024 Form 1040 Schedule D Line 14",
    )
    # California carryover: $7,500 prior, $3,000 utilized, $4,500 remaining
    ca_service.record_carryover(
        workspace_id=ws.id,
        jurisdiction=Jurisdiction.CALIFORNIA,
        category="CAPITAL_LOSS",
        prior_year_cents=750000,
        utilized_current_year_cents=300000,
        prior_return_reference="2024 FTB Form 540 Schedule D Line 6",
    )

    comparisons = ca_service.analyze_carryover_differences(ws.id)
    assert len(comparisons) == 1
    c = comparisons[0]

    assert c.category == "CAPITAL_LOSS"
    assert c.federal_prior_year_cents == 750000
    assert c.california_prior_year_cents == 750000
    assert c.has_missing_prior_return is False
    assert c.prior_year_carryover_delta_cents == 0
    assert c.status == "VERIFIED_IDENTICAL"
    assert c.federal_remaining_cents == 450000
    assert c.california_remaining_cents == 450000


def test_missing_prior_return_is_never_zero(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Missing prior return carryover is None and NEVER silently defaulted to $0."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )

    # Record carryover with None (missing prior return)
    ca_service.record_carryover(
        workspace_id=ws.id,
        jurisdiction=Jurisdiction.CALIFORNIA,
        category="PASSIVE_ACTIVITY_LOSS",
        prior_year_cents=None,
    )

    comparisons = ca_service.analyze_carryover_differences(ws.id)
    assert len(comparisons) == 1
    c = comparisons[0]

    assert c.california_prior_year_cents is None
    assert c.has_missing_prior_return is True
    assert c.status == "MISSING_PRIOR_RETURN"
    assert c.prior_year_carryover_delta_cents is None

    # Schedule CA report flags missing return as an unresolved gap
    report = ca_service.compute_schedule_ca_adjustments(ws.id)
    assert report.is_ready_for_review is False
    assert any("missing prior-year return" in q.lower() for q in report.unresolved_questions)


# ---------------------------------------------------------------------------
# 5. Schedule CA (540) Compilation & TaxAct Comparison Generation
# ---------------------------------------------------------------------------
def test_schedule_ca_compilation_and_taxact_comparison(
    session: Session,
    ca_service: CaliforniaTaxService,
    tax_service: TaxWorkspaceService,
) -> None:
    """Compile Schedule CA adjustments and generate TaxAct ReconciliationItems."""
    ws = tax_service.get_or_create_workspace(
        2025,
        jurisdiction=Jurisdiction.CALIFORNIA,
        residency_status=ResidencyStatus.FULL_YEAR,
    )
    prop = tax_service.create_rental_property(ws.id, "789 Pine St", "Bakersfield", "CA", "93301")

    # Add bonus depreciation asset ($320 subtraction)
    ca_service.record_depreciable_asset(
        workspace_id=ws.id,
        property_id=prop.id,
        description="Carpet & Flooring",
        date_placed_in_service="2023-06-01",
        cost_basis_cents=500000,
        recovery_period_months=60,
        federal_prior_depreciation_cents=400000,
        california_prior_depreciation_cents=200000,
        federal_current_depreciation_cents=50000,
        california_current_depreciation_cents=100000,
    )

    report = ca_service.compute_schedule_ca_adjustments(ws.id)
    assert report.is_ready_for_review is True
    assert report.total_subtractions_col_b_cents == 50000
    assert report.total_additions_col_c_cents == 0
    assert report.net_california_adjustment_cents == -50000

    # Generate TaxAct reconciliation items: exact match
    items = ca_service.generate_taxact_reconciliation_items(
        workspace_id=ws.id,
        taxact_entered_cents_map={"schedule_ca:depreciation_adjustment": -50000},
    )
    assert len(items) == 1
    it = items[0]
    assert it.tax_section == "California > Schedule CA (540)"
    assert it.field_key == "schedule_ca:depreciation_adjustment"
    assert it.source_working_paper_cents == -50000
    assert it.taxact_entered_cents == -50000
    assert it.delta_cents == 0
    assert it.status == ReconciliationStatus.MATCHED
    assert "17250" in it.authority_citation

    # Generate TaxAct reconciliation items: discrepancy
    items_disc = ca_service.generate_taxact_reconciliation_items(
        workspace_id=ws.id,
        taxact_entered_cents_map={"schedule_ca:depreciation_adjustment": 0},
    )
    assert len(items_disc) == 1
    it_d = items_disc[0]
    assert it_d.delta_cents == 50000
    assert it_d.status == ReconciliationStatus.DISCREPANCY
