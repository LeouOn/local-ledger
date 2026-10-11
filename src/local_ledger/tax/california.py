"""California Tax Differences, Sourcing, and Prior-Year Carryovers Service (TAX-15).

Provides:
1. Applicable-year official authority registry seeded from California Revenue & Taxation Code (R&TC),
   Franchise Tax Board (FTB) publications, and federal conformity/nonconformity rules.
2. Residency status evaluation (FULL_YEAR, PART_YEAR, NON_RESIDENT, UNKNOWN) and real property sourcing facts:
   - UNKNOWN residency raises a blocker and prevents marking California treatment verified.
   - Nonresident excludes out-of-state rental property; in-state rental property is always 100% CA source.
   - Part-year requires residency period allocation (Form 540NR).
3. Separate federal vs. California asset depreciation tracking (FTB 3885A vs Form 4562):
   - California non-conformity to federal bonus depreciation (IRC § 168(k)).
   - Schedule CA (540) Column B (subtractions) and Column C (additions) adjustments.
   - Adjusted basis upon asset disposition tracking.
4. Federal vs. California prior-year carryovers (Schedule D / FTB 3801):
   - Capital loss carryovers (annual $3,000 ordinary income offset limit).
   - Suspended passive activity rental losses (Form 8582 vs FTB 3801).
   - Invariant: Missing prior-year returns (None) are NEVER silently coerced to $0.
5. Schedule CA (540) adjustments summary and TaxAct comparison generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlmodel import Session, select

from local_ledger.tax.models import (
    AssetDepreciation,
    Jurisdiction,
    JurisdictionCarryover,
    ReconciliationItem,
    ReconciliationStatus,
    ResidencyStatus,
    TaxProperty,
    TaxWorkspace,
    WorkspaceStatus,
)
from local_ledger.tax.service import EntityNotFoundError, TaxWorkspaceService


class AdjustmentType(StrEnum):
    """Schedule CA (540) adjustment column mapping."""

    SUBTRACTION_COL_B = "SUBTRACTION_COL_B"  # Subtraction from Federal AGI (CA deduction higher or income exempt)
    ADDITION_COL_C = "ADDITION_COL_C"        # Addition to Federal AGI (Fed deduction higher or income taxable in CA)
    NO_DIFFERENCE = "NO_DIFFERENCE"          # Full conformity, no adjustment required
    DEPENDS_ON_FACTS = "DEPENDS_ON_FACTS"    # Conditional on residency, property location, or elections


@dataclass(frozen=True)
class CaliforniaTaxAuthority:
    """Official statutory authority and filing reference for California tax differences."""

    topic_id: str
    title: str
    statutory_authority: str
    ftb_form: str
    irs_form: str
    schedule_ca_line: str
    adjustment_type: AdjustmentType
    summary: str
    official_citation_notes: str


# ---------------------------------------------------------------------------
# Seeded Authority Registry (2025 California Tax Rules)
# ---------------------------------------------------------------------------
OFFICIAL_2025_CA_AUTHORITIES: dict[str, CaliforniaTaxAuthority] = {
    "BONUS_DEPRECIATION": CaliforniaTaxAuthority(
        topic_id="BONUS_DEPRECIATION",
        title="Bonus Depreciation Nonconformity",
        statutory_authority="R&TC §§ 17201 & 17250 / IRC § 168(k)",
        ftb_form="FTB 3885A",
        irs_form="Form 4562",
        schedule_ca_line="Part I Section B, Line 8f (Depreciation and Amortization)",
        adjustment_type=AdjustmentType.DEPENDS_ON_FACTS,
        summary=(
            "California does not conform to federal bonus depreciation under IRC § 168(k). "
            "Assets with federal bonus depreciation must be depreciated under standard California "
            "MACRS / modified straight line without bonus. In the year placed in service, an addition "
            "(Column C) is required; in subsequent recovery years, higher California depreciation requires "
            "a subtraction (Column B)."
        ),
        official_citation_notes="FTB Pub 1001 (Supplemental Guidelines); FTB Form 3885A Instructions.",
    ),
    "CAPITAL_LOSS_CARRYOVER": CaliforniaTaxAuthority(
        topic_id="CAPITAL_LOSS_CARRYOVER",
        title="Capital Loss Deduction & Carryovers",
        statutory_authority="R&TC § 18151 / IRC § 1211 & § 1212",
        ftb_form="FTB 540 Schedule D",
        irs_form="Form 1040 Schedule D",
        schedule_ca_line="Part I Section B, Line 7 (Capital Gain or Loss)",
        adjustment_type=AdjustmentType.DEPENDS_ON_FACTS,
        summary=(
            "California conforms to the annual $3,000 capital loss deduction against ordinary income "
            "($1,500 if married filing separately). However, California taxes capital gains as ordinary income "
            "(no preferential long-term capital gain rates). Prior-year carryovers can differ if prior-year net "
            "losses differed or if taxpayer was a part-year resident/nonresident when losses occurred."
        ),
        official_citation_notes="FTB Schedule D (540) Instructions; R&TC § 18151.",
    ),
    "PASSIVE_RENTAL_LOSS": CaliforniaTaxAuthority(
        topic_id="PASSIVE_RENTAL_LOSS",
        title="Passive Activity Rental Loss Limitations",
        statutory_authority="R&TC § 17551 / IRC § 469",
        ftb_form="FTB 3801",
        irs_form="Form 8582",
        schedule_ca_line="Part I Section B, Line 8c (Rental Real Estate, Royalties, Partnerships, etc.)",
        adjustment_type=AdjustmentType.DEPENDS_ON_FACTS,
        summary=(
            "California conforms to IRC § 469 passive activity loss rules, including the special $25,000 "
            "allowance for active participation in rental real estate phasing out between $100,000 and "
            "$150,000 MAGI. However, California MAGI uses California AGI before passive losses, which differs "
            "from federal MAGI due to state additions and subtractions. Suspended losses are tracked on FTB 3801."
        ),
        official_citation_notes="FTB 3801 Instructions (Passive Activity Loss Limitations).",
    ),
    "RESIDENTIAL_RENTAL_DEPRECIATION": CaliforniaTaxAuthority(
        topic_id="RESIDENTIAL_RENTAL_DEPRECIATION",
        title="Residential Real Estate Structure Depreciation",
        statutory_authority="R&TC § 17250 / IRC § 168(c)",
        ftb_form="FTB 3885A",
        irs_form="Form 4562",
        schedule_ca_line="Part I Section B, Line 8f (Depreciation and Amortization)",
        adjustment_type=AdjustmentType.NO_DIFFERENCE,
        summary=(
            "California conforms to federal 27.5-year straight-line depreciation for residential rental "
            "structures (excluding non-depreciable land). No adjustment is required on Schedule CA unless "
            "the acquisition basis, land/building allocation, or prior-year recovery method differed."
        ),
        official_citation_notes="R&TC § 17250(a)(1); FTB Pub 1001.",
    ),
    "SECTION_179_EXPENSE": CaliforniaTaxAuthority(
        topic_id="SECTION_179_EXPENSE",
        title="Section 179 Expense Deduction Dollar Limit",
        statutory_authority="R&TC § 17255 / IRC § 179",
        ftb_form="FTB 3885A",
        irs_form="Form 4562",
        schedule_ca_line="Part I Section B, Line 8f",
        adjustment_type=AdjustmentType.ADDITION_COL_C,
        summary=(
            "California limits the Section 179 deduction to $25,000 with a phaseout threshold beginning "
            "at $200,000. Federal Section 179 limits exceed $1,000,000. Any excess claimed federally must "
            "be added back on Schedule CA (Column C) and depreciated under standard California rules."
        ),
        official_citation_notes="R&TC § 17255; FTB 3885A Instructions.",
    ),
    "MUNICIPAL_BOND_INTEREST": CaliforniaTaxAuthority(
        topic_id="MUNICIPAL_BOND_INTEREST",
        title="Out-of-State Municipal Bond Interest",
        statutory_authority="R&TC § 17143 / Cal. Const. art. XIII § 26",
        ftb_form="Form 540",
        irs_form="Form 1040",
        schedule_ca_line="Part I Section A, Line 2b (Taxable Interest)",
        adjustment_type=AdjustmentType.ADDITION_COL_C,
        summary=(
            "Interest from municipal bonds issued by other states or their political subdivisions is "
            "exempt federally under IRC § 103, but is fully taxable in California. Requires Column C "
            "addition on Schedule CA (540). California in-state municipal bonds remain tax-exempt."
        ),
        official_citation_notes="R&TC § 17143; FTB Pub 1001.",
    ),
    "HSA_NONCONFORMITY": CaliforniaTaxAuthority(
        topic_id="HSA_NONCONFORMITY",
        title="Health Savings Account (HSA) Nonconformity",
        statutory_authority="R&TC § 17215 / IRC § 223",
        ftb_form="Form 540",
        irs_form="Form 1040 Schedule 1",
        schedule_ca_line="Part I Section C, Line 13 (HSA Deduction)",
        adjustment_type=AdjustmentType.ADDITION_COL_C,
        summary=(
            "California does not conform to IRC § 223 for Health Savings Accounts. Contributions deducted "
            "on federal Schedule 1 must be added back as Column C additions on Schedule CA (540). Interest "
            "and dividend earnings inside the HSA are also taxable annually in California."
        ),
        official_citation_notes="R&TC § 17215; FTB Pub 1001.",
    ),
    "US_OBLIGATION_INTEREST": CaliforniaTaxAuthority(
        topic_id="US_OBLIGATION_INTEREST",
        title="U.S. Treasury and Obligation Interest Exemption",
        statutory_authority="31 U.S.C. § 3124 / R&TC § 17131",
        ftb_form="Form 540",
        irs_form="Form 1040",
        schedule_ca_line="Part I Section A, Line 2b (Taxable Interest)",
        adjustment_type=AdjustmentType.SUBTRACTION_COL_B,
        summary=(
            "Interest on U.S. government obligations (Treasury bills, notes, bonds) is taxable federally "
            "but immune from state taxation under federal law. Requires Column B subtraction on Schedule CA."
        ),
        official_citation_notes="31 U.S.C. § 3124; FTB Pub 1001.",
    ),
}


# ---------------------------------------------------------------------------
# Data Transfer / Analysis Classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResidencySourcingAnalysis:
    """Analysis of taxpayer residency and property location sourcing rules."""

    residency_status: str
    is_determined: bool
    can_verify_california: bool
    sourcing_summary: str
    property_sourcing: dict[str, dict[str, Any]]
    blocking_questions: list[str]
    notes: list[str]


@dataclass(frozen=True)
class DepreciationDifference:
    """Federal vs. California depreciation and basis for a depreciable asset."""

    asset_id: str
    property_id: str
    property_address: str
    description: str
    cost_basis_cents: int
    land_value_cents: int
    depreciable_basis_cents: int
    recovery_years: float
    federal_prior_depreciation_cents: int
    california_prior_depreciation_cents: int
    federal_current_depreciation_cents: int
    california_current_depreciation_cents: int
    current_year_delta_cents: int  # california_current - federal_current
    schedule_ca_column: AdjustmentType
    adjustment_amount_cents: int
    federal_remaining_basis_cents: int
    california_remaining_basis_cents: int
    basis_difference_cents: int  # california_remaining - federal_remaining
    state_difference_reason: str
    statutory_authority: str


@dataclass(frozen=True)
class CarryoverComparison:
    """Federal vs. California prior-year carryovers comparison."""

    category: str
    property_id: str | None
    federal_prior_year_cents: int | None
    california_prior_year_cents: int | None
    has_missing_prior_return: bool
    federal_utilized_cents: int
    california_utilized_cents: int
    federal_remaining_cents: int | None
    california_remaining_cents: int | None
    prior_year_carryover_delta_cents: int | None  # CA - Fed prior
    federal_reference: str
    california_reference: str
    status: str  # VERIFIED, UNRESOLVED, MISSING_PRIOR_RETURN
    notes: str
    authority_citation: str


@dataclass(frozen=True)
class ScheduleCAAdjustmentLine:
    """Single Schedule CA (540) Column B or Column C adjustment."""

    topic_id: str
    title: str
    schedule_ca_line: str
    subtraction_col_b_cents: int
    addition_col_c_cents: int
    net_adjustment_cents: int  # addition_col_c - subtraction_col_b
    reason: str
    authority_citation: str
    supporting_asset_or_record_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScheduleCAAdjustmentReport:
    """Comprehensive compilation of all California Schedule CA adjustments."""

    tax_year: int
    residency_status: str
    adjustments: list[ScheduleCAAdjustmentLine]
    total_subtractions_col_b_cents: int
    total_additions_col_c_cents: int
    net_california_adjustment_cents: int
    depreciation_differences: list[DepreciationDifference]
    carryover_comparisons: list[CarryoverComparison]
    unresolved_questions: list[str]
    is_ready_for_review: bool


# ---------------------------------------------------------------------------
# California Tax Domain Service
# ---------------------------------------------------------------------------
class CaliforniaTaxService:
    """Service governing federal vs. California differences, carryovers, and Schedule CA audits."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -----------------------------------------------------------------------
    # Authority Registry
    # -----------------------------------------------------------------------
    @staticmethod
    def get_all_authorities() -> list[CaliforniaTaxAuthority]:
        """Return all registered official California tax authorities."""
        return list(OFFICIAL_2025_CA_AUTHORITIES.values())

    @staticmethod
    def get_authority(topic_id: str) -> CaliforniaTaxAuthority | None:
        """Retrieve authority record for a specific topic."""
        return OFFICIAL_2025_CA_AUTHORITIES.get(topic_id)

    # -----------------------------------------------------------------------
    # Residency & Property Location Sourcing
    # -----------------------------------------------------------------------
    def analyze_residency_and_sourcing(self, workspace_id: str) -> ResidencySourcingAnalysis:
        """Evaluate taxpayer residency status and determine real estate sourcing rules.

        Invariants:
        - If residency is UNKNOWN, does NOT assume FULL_YEAR. Flags as unresolved blocker.
        - Cannot mark California tax treatment as verified while residency is UNKNOWN.
        - California real property is ALWAYS 100% California-source income regardless of residency.
        - Out-of-state real property is 0% CA source for nonresidents.
        """
        workspace = self.session.get(TaxWorkspace, workspace_id)
        if workspace is None:
            raise EntityNotFoundError(f"TaxWorkspace {workspace_id} not found")

        res_status = workspace.residency_status
        blocking_questions: list[str] = []
        notes: list[str] = []

        is_determined = res_status != ResidencyStatus.UNKNOWN
        can_verify = is_determined

        if not is_determined:
            blocking_questions.append(
                "Residency status is UNKNOWN. California tax treatment cannot be verified "
                "until residency (Full-Year Resident, Part-Year Resident, or Nonresident) is confirmed."
            )
            sourcing_summary = (
                "Taxpayer residency is unconfirmed. Cannot determine whether out-of-state income "
                "is subject to California worldwide taxation."
            )
        elif res_status == ResidencyStatus.FULL_YEAR:
            sourcing_summary = (
                "Full-Year California Resident: Taxable on worldwide income from all sources. "
                "Rental income/losses from all properties (in-state and out-of-state) must be reported "
                "on Form 540. Taxes paid to other states may qualify for Schedule S credit."
            )
            notes.append("Worldwide income taxed under R&TC § 17041.")
        elif res_status == ResidencyStatus.NON_RESIDENT:
            sourcing_summary = (
                "Nonresident: Taxable ONLY on income derived from California sources (R&TC § 17951). "
                "California rental real estate is 100% California-sourced. Out-of-state rental real estate "
                "is 0% California-sourced and excluded from California taxable income."
            )
            notes.append("Sourced income taxed under R&TC § 17951; Form 540NR required.")
        elif res_status == ResidencyStatus.PART_YEAR:
            sourcing_summary = (
                "Part-Year Resident: Taxable on all income received while a resident, plus California-sourced "
                "income received while a nonresident (Form 540NR). Dual residency period allocation required."
            )
            blocking_questions.append(
                "Part-Year residency requires specific California residency start and end dates "
                "for Form 540NR dual-residency allocation between resident and nonresident periods."
            )
            can_verify = False
        else:
            sourcing_summary = f"Custom residency status '{res_status}' requires manual review."

        # Analyze properties linked to workspace
        properties = list(
            self.session.exec(
                select(TaxProperty).where(TaxProperty.workspace_id == workspace_id)
            ).all()
        )
        prop_sourcing: dict[str, dict[str, Any]] = {}

        for prop in properties:
            state_code = (prop.state or "").strip().upper()
            is_ca_prop = state_code == "CA"

            if is_ca_prop:
                allocation_pct = 100
                prop_note = "Property located in California: 100% California-source income under R&TC § 17951."
            elif res_status == ResidencyStatus.FULL_YEAR:
                allocation_pct = 100
                prop_note = (
                    f"Property located in {state_code}: Taxable in California due to full-year worldwide taxation. "
                    "May qualify for Schedule S other state tax credit if taxed by source state."
                )
            elif res_status == ResidencyStatus.NON_RESIDENT:
                allocation_pct = 0
                prop_note = (
                    f"Property located in {state_code}: Nonresident owner; 0% California-source income. "
                    "Excluded from California taxable income."
                )
            elif res_status == ResidencyStatus.PART_YEAR:
                allocation_pct = 0  # Requires date-based allocation
                prop_note = (
                    f"Property located in {state_code}: Requires period allocation for dates while resident."
                )
            else:
                allocation_pct = 0
                prop_note = (
                    f"Property located in {state_code}: Sourcing cannot be determined while residency is UNKNOWN."
                )

            prop_sourcing[prop.id] = {
                "property_id": prop.id,
                "address": f"{prop.street_address}, {prop.city}, {prop.state} {prop.zip_code}",
                "state": state_code,
                "is_california_property": is_ca_prop,
                "allocation_percentage": allocation_pct,
                "notes": prop_note,
            }

        return ResidencySourcingAnalysis(
            residency_status=res_status,
            is_determined=is_determined,
            can_verify_california=can_verify,
            sourcing_summary=sourcing_summary,
            property_sourcing=prop_sourcing,
            blocking_questions=blocking_questions,
            notes=notes,
        )

    # -----------------------------------------------------------------------
    # Asset Depreciation Differences (FTB 3885A)
    # -----------------------------------------------------------------------
    def record_depreciable_asset(
        self,
        workspace_id: str,
        property_id: str,
        description: str,
        date_placed_in_service: str,
        cost_basis_cents: int,
        land_value_cents: int = 0,
        depreciable_basis_cents: int | None = None,
        recovery_period_months: int = 330,
        federal_prior_depreciation_cents: int = 0,
        california_prior_depreciation_cents: int = 0,
        federal_current_depreciation_cents: int = 0,
        california_current_depreciation_cents: int = 0,
        state_difference_reason: str = "MACRS vs CA Modified S/L",
    ) -> AssetDepreciation:
        """Register or update an asset depreciation tracking record."""
        workspace = self.session.get(TaxWorkspace, workspace_id)
        if workspace is None:
            raise EntityNotFoundError(f"TaxWorkspace {workspace_id} not found")
        if workspace.status == WorkspaceStatus.LOCKED:
            raise ValueError(f"Workspace {workspace_id} is LOCKED and immutable")

        dep_basis = (
            depreciable_basis_cents
            if depreciable_basis_cents is not None
            else (cost_basis_cents - land_value_cents)
        )

        asset = AssetDepreciation(
            workspace_id=workspace_id,
            property_id=property_id,
            description=description,
            date_placed_in_service=date_placed_in_service,
            cost_basis_cents=cost_basis_cents,
            land_value_cents=land_value_cents,
            depreciable_basis_cents=dep_basis,
            recovery_period_months=recovery_period_months,
            prior_depreciation_fed_cents=federal_prior_depreciation_cents,
            prior_depreciation_ca_cents=california_prior_depreciation_cents,
            current_depreciation_fed_cents=federal_current_depreciation_cents,
            current_depreciation_ca_cents=california_current_depreciation_cents,
            state_difference_reason=state_difference_reason,
        )
        self.session.add(asset)
        workspace_svc = TaxWorkspaceService(self.session)
        workspace_svc._invalidate_downstream(workspace_id)
        self.session.commit()
        self.session.refresh(asset)
        return asset

    def analyze_depreciation_differences(
        self, workspace_id: str
    ) -> list[DepreciationDifference]:
        """Compute asset-by-asset federal vs. California depreciation differences.

        Determines:
        - Current year delta (CA depreciation - Fed depreciation).
        - Schedule CA adjustment:
          - If CA > Fed: Column B subtraction (higher state expense deduction).
          - If Fed > CA: Column C addition (higher federal expense deduction).
          - If CA == Fed: No difference.
        - Cumulative remaining basis difference (crucial upon asset disposition).
        """
        stmt = (
            select(AssetDepreciation)
            .where(AssetDepreciation.workspace_id == workspace_id)
            .order_by(AssetDepreciation.date_placed_in_service)  # type: ignore[attr-defined]
        )
        assets = list(self.session.exec(stmt).all())
        results: list[DepreciationDifference] = []

        prop_map: dict[str, str] = {}
        for a in assets:
            if a.property_id not in prop_map:
                prop = self.session.get(TaxProperty, a.property_id)
                prop_map[a.property_id] = (
                    f"{prop.street_address}, {prop.city}" if prop else a.property_id
                )

            fed_curr = a.current_depreciation_fed_cents
            ca_curr = a.current_depreciation_ca_cents
            delta = ca_curr - fed_curr

            if delta > 0:
                col = AdjustmentType.SUBTRACTION_COL_B
                adj_amt = delta
            elif delta < 0:
                col = AdjustmentType.ADDITION_COL_C
                adj_amt = abs(delta)
            else:
                col = AdjustmentType.NO_DIFFERENCE
                adj_amt = 0

            # Remaining basis calculation
            total_fed_dep = a.prior_depreciation_fed_cents + fed_curr
            total_ca_dep = a.prior_depreciation_ca_cents + ca_curr
            fed_rem_basis = max(0, a.depreciable_basis_cents - total_fed_dep)
            ca_rem_basis = max(0, a.depreciable_basis_cents - total_ca_dep)
            basis_diff = ca_rem_basis - fed_rem_basis

            auth = OFFICIAL_2025_CA_AUTHORITIES.get(
                "BONUS_DEPRECIATION"
                if "bonus" in a.state_difference_reason.lower()
                else "RESIDENTIAL_RENTAL_DEPRECIATION"
            )
            citation = auth.statutory_authority if auth else "R&TC § 17250 / FTB 3885A"

            results.append(
                DepreciationDifference(
                    asset_id=a.id,
                    property_id=a.property_id,
                    property_address=prop_map.get(a.property_id, a.property_id),
                    description=a.description,
                    cost_basis_cents=a.cost_basis_cents,
                    land_value_cents=a.land_value_cents,
                    depreciable_basis_cents=a.depreciable_basis_cents,
                    recovery_years=a.recovery_years,
                    federal_prior_depreciation_cents=a.prior_depreciation_fed_cents,
                    california_prior_depreciation_cents=a.prior_depreciation_ca_cents,
                    federal_current_depreciation_cents=fed_curr,
                    california_current_depreciation_cents=ca_curr,
                    current_year_delta_cents=delta,
                    schedule_ca_column=col,
                    adjustment_amount_cents=adj_amt,
                    federal_remaining_basis_cents=fed_rem_basis,
                    california_remaining_basis_cents=ca_rem_basis,
                    basis_difference_cents=basis_diff,
                    state_difference_reason=a.state_difference_reason,
                    statutory_authority=citation,
                )
            )

        return results

    # -----------------------------------------------------------------------
    # Prior-Year Carryovers Tracker
    # -----------------------------------------------------------------------
    def record_carryover(
        self,
        workspace_id: str,
        jurisdiction: str,
        category: str,
        prior_year_cents: int | None,
        utilized_current_year_cents: int = 0,
        remaining_carryover_cents: int | None = None,
        prior_return_reference: str = "",
        property_id: str | None = None,
    ) -> JurisdictionCarryover:
        """Record or update a federal or California prior-year carryover.

        Invariant: prior_year_cents=None indicates missing prior return.
        Never default missing return to 0.
        """
        workspace = self.session.get(TaxWorkspace, workspace_id)
        if workspace is None:
            raise EntityNotFoundError(f"TaxWorkspace {workspace_id} not found")
        if workspace.status == WorkspaceStatus.LOCKED:
            raise ValueError(f"Workspace {workspace_id} is LOCKED and immutable")

        # Compute remaining carryover if prior year is known and remaining is None
        rem_cents = remaining_carryover_cents
        if rem_cents is None and prior_year_cents is not None:
            rem_cents = max(0, prior_year_cents - utilized_current_year_cents)

        carryover = JurisdictionCarryover(
            workspace_id=workspace_id,
            jurisdiction=jurisdiction,
            category=category,
            property_id=property_id,
            prior_year_cents=prior_year_cents,
            utilized_current_year_cents=utilized_current_year_cents,
            remaining_carryover_cents=rem_cents,
            prior_return_reference=prior_return_reference,
        )
        self.session.add(carryover)
        workspace_svc = TaxWorkspaceService(self.session)
        workspace_svc._invalidate_downstream(workspace_id)
        self.session.commit()
        self.session.refresh(carryover)
        return carryover

    def analyze_carryover_differences(
        self, workspace_id: str
    ) -> list[CarryoverComparison]:
        """Pair federal and California carryovers and evaluate differences.

        Handles:
        - CAPITAL_LOSS: Form 1040 Schedule D vs FTB Form 540 Schedule D.
        - PASSIVE_ACTIVITY_LOSS: Form 8582 vs FTB Form 3801.
        - MISSING_PRIOR_RETURN: Flags missing prior return without guessing.
        """
        stmt = (
            select(JurisdictionCarryover)
            .where(JurisdictionCarryover.workspace_id == workspace_id)
            .order_by(JurisdictionCarryover.category)  # type: ignore[attr-defined]
        )
        carryovers = list(self.session.exec(stmt).all())

        # Group carryovers by (category, property_id)
        grouped: dict[tuple[str, str | None], dict[str, JurisdictionCarryover]] = {}
        for c in carryovers:
            key = (c.category, c.property_id)
            grouped.setdefault(key, {})[c.jurisdiction] = c

        results: list[CarryoverComparison] = []

        for (cat, prop_id), by_jur in grouped.items():
            fed = by_jur.get(Jurisdiction.FEDERAL)
            ca = by_jur.get(Jurisdiction.CALIFORNIA)

            fed_prior = fed.prior_year_cents if fed else None
            ca_prior = ca.prior_year_cents if ca else None

            has_missing = fed_prior is None or ca_prior is None

            fed_utilized = fed.utilized_current_year_cents if fed else 0
            ca_utilized = ca.utilized_current_year_cents if ca else 0

            fed_rem = fed.remaining_carryover_cents if fed else None
            ca_rem = ca.remaining_carryover_cents if ca else None

            delta = (
                (ca_prior - fed_prior)
                if (ca_prior is not None and fed_prior is not None)
                else None
            )

            fed_ref = fed.prior_return_reference if fed else ""
            ca_ref = ca.prior_return_reference if ca else ""

            if cat == "CAPITAL_LOSS":
                auth = OFFICIAL_2025_CA_AUTHORITIES["CAPITAL_LOSS_CARRYOVER"]
                citation = auth.statutory_authority
                if not fed_ref:
                    fed_ref = "2024 Form 1040 Schedule D Capital Loss Carryover Worksheet line 14"
                if not ca_ref:
                    ca_ref = "2024 FTB Form 540 Schedule D line 6"
            elif cat == "PASSIVE_ACTIVITY_LOSS":
                auth = OFFICIAL_2025_CA_AUTHORITIES["PASSIVE_RENTAL_LOSS"]
                citation = auth.statutory_authority
                if not fed_ref:
                    fed_ref = "2024 Form 8582 Line 1c / Worksheet 5"
                if not ca_ref:
                    ca_ref = "2024 FTB 3801 Side 1 / Worksheet 5"
            else:
                citation = "R&TC / IRC Carryover Rules"

            if has_missing:
                status = "MISSING_PRIOR_RETURN"
                notes = (
                    "Prior-year tax return is missing or unverified. Carryover amounts cannot be "
                    "assumed to be 0 or verified without prior return evidence."
                )
            elif delta != 0:
                status = "DIFFERENCE_IDENTIFIED"
                notes = f"Federal and California carryovers differ by ${abs(delta or 0) / 100:,.2f}."
            else:
                status = "VERIFIED_IDENTICAL"
                notes = "Federal and California carryover amounts are identical."

            results.append(
                CarryoverComparison(
                    category=cat,
                    property_id=prop_id,
                    federal_prior_year_cents=fed_prior,
                    california_prior_year_cents=ca_prior,
                    has_missing_prior_return=has_missing,
                    federal_utilized_cents=fed_utilized,
                    california_utilized_cents=ca_utilized,
                    federal_remaining_cents=fed_rem,
                    california_remaining_cents=ca_rem,
                    prior_year_carryover_delta_cents=delta,
                    federal_reference=fed_ref,
                    california_reference=ca_ref,
                    status=status,
                    notes=notes,
                    authority_citation=citation,
                )
            )

        return results

    # -----------------------------------------------------------------------
    # Schedule CA (540) Adjustments Synthesis
    # -----------------------------------------------------------------------
    def compute_schedule_ca_adjustments(
        self, workspace_id: str
    ) -> ScheduleCAAdjustmentReport:
        """Synthesize all federal/California differences into Schedule CA (540) adjustments."""
        workspace = self.session.get(TaxWorkspace, workspace_id)
        if workspace is None:
            raise EntityNotFoundError(f"TaxWorkspace {workspace_id} not found")

        adjustments: list[ScheduleCAAdjustmentLine] = []
        unresolved: list[str] = []

        # 1. Residency Sourcing Check
        res_analysis = self.analyze_residency_and_sourcing(workspace_id)
        if not res_analysis.is_determined:
            unresolved.extend(res_analysis.blocking_questions)

        # 2. Depreciation Differences
        dep_diffs = self.analyze_depreciation_differences(workspace_id)
        sub_dep_cents = sum(
            d.adjustment_amount_cents
            for d in dep_diffs
            if d.schedule_ca_column == AdjustmentType.SUBTRACTION_COL_B
        )
        add_dep_cents = sum(
            d.adjustment_amount_cents
            for d in dep_diffs
            if d.schedule_ca_column == AdjustmentType.ADDITION_COL_C
        )

        if sub_dep_cents > 0 or add_dep_cents > 0:
            auth_dep = OFFICIAL_2025_CA_AUTHORITIES["BONUS_DEPRECIATION"]
            adjustments.append(
                ScheduleCAAdjustmentLine(
                    topic_id="DEPRECIATION_ADJUSTMENT",
                    title="Depreciation and Amortization (FTB 3885A)",
                    schedule_ca_line=auth_dep.schedule_ca_line,
                    subtraction_col_b_cents=sub_dep_cents,
                    addition_col_c_cents=add_dep_cents,
                    net_adjustment_cents=add_dep_cents - sub_dep_cents,
                    reason=(
                        "Adjustment for nonconformity to federal bonus depreciation (IRC § 168(k)) "
                        "and differing recovery schedules under California R&TC § 17250."
                    ),
                    authority_citation=auth_dep.statutory_authority,
                    supporting_asset_or_record_ids=[d.asset_id for d in dep_diffs],
                )
            )

        # 3. Carryover Differences
        carryovers = self.analyze_carryover_differences(workspace_id)
        for c in carryovers:
            if c.has_missing_prior_return:
                unresolved.append(
                    f"Missing prior-year return for {c.category} carryover verification."
                )
            elif (
                c.prior_year_carryover_delta_cents is not None
                and c.prior_year_carryover_delta_cents != 0
            ):
                # If current year utilization differs between Fed and CA:
                util_delta = c.california_utilized_cents - c.federal_utilized_cents
                if util_delta > 0:
                    sub_c = util_delta
                    add_c = 0
                else:
                    sub_c = 0
                    add_c = abs(util_delta)

                adjustments.append(
                    ScheduleCAAdjustmentLine(
                        topic_id=f"CARRYOVER_{c.category}",
                        title=f"{c.category.replace('_', ' ').title()} Carryover Difference",
                        schedule_ca_line="Part I Section B, Line 7 (Schedule D)",
                        subtraction_col_b_cents=sub_c,
                        addition_col_c_cents=add_c,
                        net_adjustment_cents=add_c - sub_c,
                        reason=f"Federal and California carryovers differ: {c.notes}",
                        authority_citation=c.authority_citation,
                    )
                )

        total_sub = sum(a.subtraction_col_b_cents for a in adjustments)
        total_add = sum(a.addition_col_c_cents for a in adjustments)
        net_adj = total_add - total_sub

        is_ready = len(unresolved) == 0 and res_analysis.can_verify_california

        return ScheduleCAAdjustmentReport(
            tax_year=workspace.tax_year,
            residency_status=workspace.residency_status,
            adjustments=adjustments,
            total_subtractions_col_b_cents=total_sub,
            total_additions_col_c_cents=total_add,
            net_california_adjustment_cents=net_adj,
            depreciation_differences=dep_diffs,
            carryover_comparisons=carryovers,
            unresolved_questions=unresolved,
            is_ready_for_review=is_ready,
        )

    # -----------------------------------------------------------------------
    # TaxAct Reconciliation Comparison Items
    # -----------------------------------------------------------------------
    def generate_taxact_reconciliation_items(
        self,
        workspace_id: str,
        taxact_entered_cents_map: dict[str, int] | None = None,
    ) -> list[ReconciliationItem]:
        """Generate audited comparison items between working-paper adjustments and TaxAct inputs.

        Maps each Schedule CA line to a ReconciliationItem. If TaxAct input is supplied,
        computes delta and status (MATCHED vs DISCREPANCY vs UNRESOLVED).
        """
        report = self.compute_schedule_ca_adjustments(workspace_id)
        entered_map = taxact_entered_cents_map or {}
        items: list[ReconciliationItem] = []

        workspace_svc = TaxWorkspaceService(self.session)

        for adj in report.adjustments:
            field_key = f"schedule_ca:{adj.topic_id.lower()}"
            taxact_amt = entered_map.get(field_key, 0)
            wp_amt = adj.net_adjustment_cents
            delta = taxact_amt - wp_amt

            if not report.is_ready_for_review:
                status = ReconciliationStatus.UNRESOLVED
                explanation = f"Pending resolution of prerequisites: {'; '.join(report.unresolved_questions)}"
            elif delta == 0 and field_key in entered_map:
                status = ReconciliationStatus.MATCHED
                explanation = f"Exact match with TaxAct input ({adj.title})."
            elif field_key in entered_map:
                status = ReconciliationStatus.DISCREPANCY
                explanation = f"Discrepancy of ${abs(delta) / 100:,.2f} between working paper and TaxAct."
            else:
                status = ReconciliationStatus.UNRESOLVED
                explanation = f"TaxAct entered figure has not been provided yet for {adj.title}."

            item = workspace_svc.record_reconciliation_item(
                workspace_id=workspace_id,
                tax_section="California > Schedule CA (540)",
                field_key=field_key,
                source_working_paper_cents=wp_amt,
                taxact_entered_cents=taxact_amt,
                delta_cents=delta,
                status=status,
                explanation=explanation,
                authority_citation=adj.authority_citation,
            )
            items.append(item)

        return items
