"""Rental working papers (TAX-14): audit Schedule E inputs, exact comparisons, cited questions.

Acceptance scenarios, each from the task:

- two properties: figures stay attributed to their own property;
- mixed use: day counts preserved, allocation raised as a question, never applied;
- mortgage principal: separated, questioned, excluded from operating expenses;
- escrow double counting: equal expense/escrow amounts flagged, 1098 interest
  cross-checked;
- a possible improvement: flagged with its recorded authority, never auto-decided;
- missing prior asset schedule and missing suspended-loss history: questions,
  not zeros, and history is never inferred;
- corrected statement: superseded evidence excluded, lineage stated;
- unresolved questions prevent a property being marked fully checked;
- amounts trace to documents; bank flows can never supply them.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from sqlmodel import Session
from tests.factories import make_account, make_transaction

from local_ledger.db import init_db
from local_ledger.tax import Jurisdiction, TaxWorkspaceService
from local_ledger.tax.models import (
    AssetDepreciation,
    JurisdictionCarryover,
)
from local_ledger.tax.reconciliation import ReconciliationService
from local_ledger.tax.rentals import (
    DISCREPANCY,
    MATCHED,
    NOT_RECORDED,
    ORPHANED,
    SOURCE_DRIFT,
    RentalAuditService,
)
from local_ledger.tax.scope import ReviewScopeMode, WorkspaceScopeService


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def svc(session: Session, engine, tax_env: Path) -> TaxWorkspaceService:
    init_db(engine)
    return TaxWorkspaceService(session)


@pytest.fixture()
def ws(svc: TaxWorkspaceService) -> str:
    return svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL).id


def _doc(svc: TaxWorkspaceService, ws: str, name: str, tag: bytes) -> object:
    return svc.register_document(
        workspace_id=ws,
        file_name=name,
        source=b"%PDF-1.4 synthetic-rental-" + tag,
        doc_type="RENTAL_ANNUAL_STATEMENT",
    )


def _prop(svc: TaxWorkspaceService, ws: str, street: str, **kwargs) -> object:  # type: ignore[no-untyped-def]
    defaults = dict(
        workspace_id=ws,
        street_address=street,
        city="Pasadena",
        state="CA",
        zip_code="91101",
        property_type="Single Family Residential",
        date_placed_in_service="2021-08-01",
    )
    defaults.update(kwargs)
    return svc.create_rental_property(**defaults)


def _exp(svc: TaxWorkspaceService, ws: str, prop_id: str, category: str, cents: int, doc_id=None, **kwargs):  # type: ignore[no-untyped-def]
    row = svc.record_rental_expense(
        workspace_id=ws,
        property_id=prop_id,
        category=category,
        amount_cents=cents,
        raw_text=f"{category} ${cents / 100:,.2f}",
        source_document_id=doc_id,
        page_number=2,
        box_or_line=category,
        **kwargs,
    )
    svc.verify_rental_expense(row.id)
    return row


def _field(svc: TaxWorkspaceService, ws: str, doc_id: str, name: str, cents: int):
    f = svc.propose_field(
        workspace_id=ws,
        document_id=doc_id,
        field_name=name,
        raw_text=f"${cents / 100:,.2f}",
        amount_cents=cents,
        page_number=1,
        box_or_line=name,
    )
    svc.verify_field(f.id, verified_by="reviewer")
    return f


def _questions(paper) -> dict[str, list]:  # type: ignore[no-untyped-def]
    by_code: dict[str, list] = {}
    for q in paper.questions:
        by_code.setdefault(q.code, []).append(q)
    return by_code


def _configure_scope(session: Session, ws: str, prop_ids: list[str]) -> None:
    WorkspaceScopeService(session).configure(
        workspace_id=ws,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        property_ids=prop_ids,
        properties_confirmed=True,
    )


# ---------------------------------------------------------------------------
# two properties: attribution and isolation
# ---------------------------------------------------------------------------


def test_two_properties_keep_figures_separate(svc: TaxWorkspaceService, ws: str) -> None:
    doc_a = _doc(svc, ws, "rental_statement_a.pdf", b"a")
    doc_b = _doc(svc, ws, "rental_statement_b.pdf", b"b")
    prop_a = _prop(svc, ws, "123 Citrus Lane")
    prop_b = _prop(svc, ws, "456 Sierra Drive", date_placed_in_service="2023-01-15")

    _exp(svc, ws, prop_a.id, "Repairs", 142_000, doc_id=doc_a.id)
    _exp(svc, ws, prop_a.id, "Insurance", 185_000, doc_id=doc_a.id)
    _exp(svc, ws, prop_b.id, "Management Fees", 288_000, doc_id=doc_b.id)
    _field(svc, ws, doc_a.id, "gross_rents_received", 3_600_000)

    audit = RentalAuditService(svc.session, ws)
    papers = {p.property_id: p for p in audit.working_papers()}

    a, b = papers[prop_a.id], papers[prop_b.id]
    assert a.totals["expenses.repairs"] == 142_000
    assert a.totals["expenses.insurance"] == 185_000
    assert a.totals["total_operating_expenses"] == 327_000
    assert a.totals["rents"] == 3_600_000  # via doc_a, which prop_a's expenses reference
    assert "expenses.repairs" not in b.totals
    assert b.totals["rents"] == 0  # no rents field on doc_b
    assert b.date_placed_in_service == "2023-01-15"
    assert a.label.startswith("123 Citrus Lane")


# ---------------------------------------------------------------------------
# mortgage principal, escrow and 1098 cross-checks
# ---------------------------------------------------------------------------


def test_mortgage_principal_is_separated_and_questioned(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"c")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 50_000, doc_id=doc.id)
    _exp(svc, ws, prop.id, "Mortgage Principal", 820_000, doc_id=doc.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    codes = _questions(paper)

    (q,) = codes["MORTGAGE_PRINCIPAL_NOT_DEDUCTIBLE"]
    assert "$8,200.00" in q.message and "line 12" in q.message
    assert paper.totals["total_operating_expenses"] == 50_000  # principal excluded
    assert paper.totals["separated_not_expenses"] == 820_000  # but visible
    principal_lines = [line for line in paper.expense_lines if line.separated]
    assert principal_lines[0].citation.startswith("rental_statement.pdf p.2")


def test_escrow_double_counting_is_flagged(svc: TaxWorkspaceService, ws: str) -> None:
    rental_doc = _doc(svc, ws, "rental_statement.pdf", b"d1")
    mortgage_doc = _doc(svc, ws, "form_1098.pdf", b"d2")
    prop = _prop(svc, ws, "123 Citrus Lane")
    # Property taxes on the rental statement, and the same amount disbursed
    # from the lender's escrow - the classic double count.
    _exp(svc, ws, prop.id, "Property Taxes", 560_000, doc_id=rental_doc.id)
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=rental_doc.id)
    _exp(svc, ws, prop.id, "Mortgage Interest", 1_245_000, doc_id=mortgage_doc.id)
    _field(svc, ws, mortgage_doc.id, "mortgage_interest_1098_box1", 1_245_000)
    _field(svc, ws, mortgage_doc.id, "escrow_tax_paid_by_lender", 560_000)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    codes = _questions(paper)

    (escrow_q,) = codes["ESCROW_DOUBLE_COUNT"]
    assert "$5,600.00" in escrow_q.message and "enter the figure once" in escrow_q.message
    # Matching 1098 box 1 raises no interest question.
    assert "MORTGAGE_INTEREST_MISMATCH" not in codes


def test_mortgage_interest_mismatch_is_flagged(svc: TaxWorkspaceService, ws: str) -> None:
    rental_doc = _doc(svc, ws, "rental_statement.pdf", b"e1")
    mortgage_doc = _doc(svc, ws, "form_1098.pdf", b"e2")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=rental_doc.id)
    _exp(svc, ws, prop.id, "Mortgage Interest", 1_100_000, doc_id=mortgage_doc.id)
    _field(svc, ws, mortgage_doc.id, "mortgage_interest_1098_box1", 1_245_000)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["MORTGAGE_INTEREST_MISMATCH"]
    assert "$11,000.00" in q.message and "$12,450.00" in q.message


# ---------------------------------------------------------------------------
# capital improvement suspect, deposits, mixed use
# ---------------------------------------------------------------------------


def test_capital_improvement_suspect_surfaces_authority(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"f")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(
        svc, ws, prop.id, "Repairs", 780_000, doc_id=doc.id,
        is_capital_improvement_suspect=True,
        treatment_authority="Treas. Reg. § 1.263(a)-3(j) (Betterment/Restoration)",
    )

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["CAPITAL_IMPROVEMENT_SUSPECT"]
    assert "$7,800.00" in q.message
    assert "does not decide" in q.message
    assert "1.263(a)-3(j)" in q.citation  # the recorded authority travels with the question


def test_security_deposits_are_separated_and_questioned(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"g")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Cleaning", 65_000, doc_id=doc.id)
    _exp(svc, ws, prop.id, "Security Deposits", 300_000, doc_id=doc.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["DEPOSIT_TREATMENT"]
    assert "held in trust" in q.message
    assert paper.totals["total_operating_expenses"] == 65_000
    assert paper.totals["separated_not_expenses"] == 300_000


def test_mixed_use_preserves_day_counts_and_questions_allocation(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"h")
    prop = _prop(svc, ws, "123 Citrus Lane", days_rented=280, days_personal_use=85)
    _exp(svc, ws, prop.id, "Repairs", 100_000, doc_id=doc.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["MIXED_USE_ALLOCATION"]
    assert "280 rented" in q.message and "85 personal-use" in q.message
    # The counts are preserved and NOT applied to amounts.
    assert paper.days_rented == 280 and paper.days_personal_use == 85
    assert paper.totals["expenses.repairs"] == 100_000


def test_partial_ownership_is_flagged_not_scaled(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"i")
    prop = _prop(svc, ws, "123 Citrus Lane", ownership_basis_points=5000)
    _exp(svc, ws, prop.id, "Repairs", 100_000, doc_id=doc.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["PARTIAL_OWNERSHIP"]
    assert "50.00%" in q.message
    assert paper.totals["expenses.repairs"] == 100_000  # preserved, not halved


# ---------------------------------------------------------------------------
# depreciation history and suspended losses
# ---------------------------------------------------------------------------


def test_missing_asset_schedule_is_a_question_not_zero(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"j")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 100_000, doc_id=doc.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["MISSING_ASSET_SCHEDULE"]
    assert "not inferred" in q.message and "not zero" in q.message
    assert paper.depreciation == []


def test_zero_prior_depreciation_for_older_asset_is_suspect(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"k")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=doc.id)
    session = svc.session
    session.add(
        AssetDepreciation(
            workspace_id=ws,
            property_id=prop.id,
            description="Residential building",
            date_placed_in_service="2021-08-01",
            cost_basis_cents=40_000_000,
            depreciable_basis_cents=34_000_000,
            recovery_period_months=330,
            prior_depreciation_fed_cents=0,  # missing history masquerading as zero
        )
    )
    session.commit()

    paper = RentalAuditService(session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["SUSPECT_MISSING_DEPRECIATION_HISTORY"]
    assert "2021-08-01" in q.message and "not zero" in q.message


def test_recorded_depreciation_appears_as_context(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"l")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=doc.id)
    session = svc.session
    session.add(
        AssetDepreciation(
            workspace_id=ws,
            property_id=prop.id,
            description="Residential building",
            date_placed_in_service="2021-08-01",
            cost_basis_cents=40_000_000,
            depreciable_basis_cents=34_000_000,
            recovery_period_months=330,
            prior_depreciation_fed_cents=4_137_930,
            prior_depreciation_ca_cents=3_800_000,
        )
    )
    session.commit()

    paper = RentalAuditService(session, ws).working_paper(prop.id)
    (line,) = paper.depreciation
    assert line.prior_fed_cents == 4_137_930 and line.prior_ca_cents == 3_800_000
    assert line.recovery_period_months == 330
    assert not any(q.code.startswith("SUSPECT") for q in paper.questions)


def test_suspended_loss_history_missing_is_a_question(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"m")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=doc.id)
    session = svc.session
    session.add(
        JurisdictionCarryover(
            workspace_id=ws,
            property_id=prop.id,
            category="SUSPENDED_LOSS",
            prior_year_cents=None,
        )
    )
    session.commit()

    paper = RentalAuditService(session, ws).working_paper(prop.id)
    (q,) = _questions(paper)["MISSING_SUSPENDED_LOSS_HISTORY"]
    assert "UNKNOWN" in q.message and "not zero" in q.message


# ---------------------------------------------------------------------------
# corrected statement lineage
# ---------------------------------------------------------------------------


def test_corrected_statement_excludes_superseded_expenses(svc: TaxWorkspaceService, ws: str) -> None:
    v1 = _doc(svc, ws, "rental_v1.pdf", b"n1")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 100_000, doc_id=v1.id)
    _exp(svc, ws, prop.id, "Insurance", 185_000, doc_id=v1.id)

    v2 = svc.register_document(
        workspace_id=ws,
        file_name="rental_v2_corrected.pdf",
        source=b"%PDF-1.4 synthetic-rental-n2",
        doc_type="RENTAL_ANNUAL_STATEMENT",
        is_corrected=True,
        supersedes_doc_id=v1.id,
    )
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=v2.id)

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)

    assert paper.totals["expenses.repairs"] == 142_000  # correction only
    assert paper.totals.get("expenses.insurance", 0) == 0  # superseded, no replacement
    assert all(line.document_id == v2.id for line in paper.expense_lines)
    assert any(
        "rental_v2_corrected.pdf supersedes rental_v1.pdf" in n for n in paper.correction_notes
    )


# ---------------------------------------------------------------------------
# fully checked gate, scope, and comparisons
# ---------------------------------------------------------------------------


def test_unresolved_questions_prevent_fully_checked(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"o")
    clean = _prop(svc, ws, "1 Clean Street")
    messy = _prop(svc, ws, "2 Messy Street", days_personal_use=30)
    _exp(svc, ws, clean.id, "Repairs", 10_000, doc_id=doc.id)
    _exp(svc, ws, messy.id, "Mortgage Principal", 820_000, doc_id=doc.id)
    # Clean needs a depreciation history with priors to be genuinely clean:
    # a missing schedule is itself an unresolved question.
    svc.session.add(
        AssetDepreciation(
            workspace_id=ws,
            property_id=clean.id,
            description="Residential building",
            date_placed_in_service="2021-08-01",
            cost_basis_cents=20_000_000,
            depreciable_basis_cents=17_000_000,
            recovery_period_months=330,
            prior_depreciation_fed_cents=2_068_965,
        )
    )
    svc.session.commit()
    _configure_scope(svc.session, ws, [clean.id, messy.id])

    papers = {p.property_id: p for p in RentalAuditService(svc.session, ws).working_papers()}
    assert papers[clean.id].fully_checked is True
    assert papers[messy.id].fully_checked is False


def test_out_of_scope_property_cannot_be_fully_checked(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"p")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 10_000, doc_id=doc.id)
    # Scope configured for a different property.
    other = _prop(svc, ws, "9 Other Lane")
    _configure_scope(svc.session, ws, [other.id])

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    assert paper.in_scope is False
    assert paper.fully_checked is False
    assert _questions(paper)["SCOPE_PROPERTY_UNCOVERED"]


def test_comparisons_matched_discrepant_not_recorded(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"q")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _field(svc, ws, doc.id, "gross_rents_received", 3_600_000)
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)

    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Schedule E",
        field_key=f"{prop.id}.rents",
        source_working_paper_cents=3_600_000,
        taxact_entered_cents=3_600_000,
        delta_cents=0,
        status="MATCHED",
    )
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Schedule E",
        field_key=f"{prop.id}.expenses.repairs",
        source_working_paper_cents=142_000,
        taxact_entered_cents=132_000,  # $100 under
        delta_cents=-10_000,
        status="DISCREPANCY",
    )

    rows = {r.field_key: r for r in RentalAuditService(svc.session, ws).comparisons()}
    matched = rows[f"{prop.id}.rents"]
    assert matched.status == MATCHED and matched.delta_cents == 0
    assert matched.member_ids  # traceable to the rents field

    discrepant = rows[f"{prop.id}.expenses.repairs"]
    assert discrepant.status == DISCREPANCY and discrepant.delta_cents == -10_000
    assert "under-entered" in discrepant.note

    assert rows[f"{prop.id}.total_operating_expenses"].status == NOT_RECORDED


def test_comparison_flags_drift_and_orphans(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"r")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)

    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Schedule E",
        field_key=f"{prop.id}.expenses.insurance",  # no such figure exists
        source_working_paper_cents=185_000,
        taxact_entered_cents=185_000,
        delta_cents=0,
        status="MATCHED",
    )
    rows = {r.field_key: r for r in RentalAuditService(svc.session, ws).comparisons()}
    orphan = rows[f"{prop.id}.expenses.insurance"]
    assert orphan.status == ORPHANED and orphan.working_paper_cents is None


def test_source_drift_comparison(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"s")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Schedule E",
        field_key=f"{prop.id}.expenses.management fees",
        source_working_paper_cents=288_000,
        taxact_entered_cents=288_000,
        delta_cents=0,
        status="MATCHED",
    )
    rows = {r.field_key: r for r in RentalAuditService(svc.session, ws).comparisons()}
    assert SOURCE_DRIFT not in rows  # sanity: key not produced yet
    # Now add the real management-fee evidence at a different amount and
    # re-record; a fresh comparison against a stale recorded amount drifts.
    _exp(svc, ws, prop.id, "Management Fees", 250_000, doc_id=doc.id)
    rows = {r.field_key: r for r in RentalAuditService(svc.session, ws).comparisons()}
    # The earlier item was staled by the evidence write; record a new one
    # against the OLD amount to simulate a drifted source.
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Schedule E",
        field_key=f"{prop.id}.expenses.management fees",
        source_working_paper_cents=288_000,
        taxact_entered_cents=288_000,
        delta_cents=0,
        status="MATCHED",
    )
    rows = {r.field_key: r for r in RentalAuditService(svc.session, ws).comparisons()}
    drifted = rows[f"{prop.id}.expenses.management fees"]
    assert drifted.status == SOURCE_DRIFT
    assert drifted.working_paper_cents == 250_000
    assert "re-review" in drifted.note


# ---------------------------------------------------------------------------
# bank-flow isolation and reconciliation consistency
# ---------------------------------------------------------------------------


def test_bank_flows_never_supply_rental_figures(svc: TaxWorkspaceService, ws: str, session: Session) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"t")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _field(svc, ws, doc.id, "gross_rents_received", 3_600_000)
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)

    audit = RentalAuditService(session, ws)
    before = audit.working_paper(prop.id)

    # Rent-sized deposits and rent-sized withdrawals must not move a cent.
    account = make_account(session, name="Property Checking")
    make_transaction(session, account_id=account.id, amount_cents=3_600_000, date="2025-03-01")
    make_transaction(session, account_id=account.id, amount_cents=-1_200_000, date="2025-09-01")

    after = audit.working_paper(prop.id)
    assert after.totals == before.totals
    assert {(q.code, q.message) for q in after.questions} == {
        (q.code, q.message) for q in before.questions
    }


def test_module_reads_no_bank_flow_tables() -> None:
    """Structural guarantee: rentals.py imports neither ledger rows nor classifications."""
    from local_ledger.tax import rentals

    tree = ast.parse(inspect.getsource(rentals))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    assert not any(name in imported for name in ("Transaction", "ClassificationSubject", "TaxClassification")), (
        sorted(imported)
    )


def test_consistency_with_reconciliation_rental_totals(svc: TaxWorkspaceService, ws: str) -> None:
    """Given reviewed deductible decisions, both layers agree per property."""
    from local_ledger.tax.classification import (
        ClassificationSubject,
        TaxClassificationService,
        TaxTreatment,
    )

    doc = _doc(svc, ws, "rental_statement.pdf", b"u")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)
    _exp(svc, ws, prop.id, "Insurance", 185_000, doc_id=doc.id)
    _exp(svc, ws, prop.id, "Mortgage Principal", 820_000, doc_id=doc.id)

    _configure_scope(svc.session, ws, [prop.id])
    cls = TaxClassificationService(svc.session, ws)
    for expense, treatment, citation in (
        ("Repairs", TaxTreatment.RENTAL_REPAIRS, "IRC § 162; IRS Pub 527 (repairs)"),
        ("Insurance", TaxTreatment.RENTAL_INSURANCE, "IRC § 162; IRS Pub 527 (insurance)"),
        ("Mortgage Principal", TaxTreatment.MORTGAGE_PRINCIPAL, "IRS Pub 527 (principal not deductible)"),
    ):
        row = next(line for line in RentalAuditService(svc.session, ws).working_paper(prop.id).expense_lines if line.category == expense)
        cls.classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE,
            subject_id=row.expense_id,
            measure="AMOUNT",
            treatment=treatment,
            fact=f"{expense} per the rental statement",
            authority_citation=citation,
        )

    paper = RentalAuditService(svc.session, ws).working_paper(prop.id)
    report = ReconciliationService(svc.session, ws).rental_totals(prop.id)

    # Bucket keys are "{bucket}:{treatment}:{property_id}".
    deductible = sum(t.amount_cents for t in report.totals if t.field_key.startswith("deductible:"))
    not_deductible = sum(
        t.amount_cents for t in report.totals if t.field_key.startswith("not_deductible:")
    )
    # The reviewed-decision layer and the statement-level paper agree:
    # principal is visibly not an expense in both, everything else deductible.
    assert deductible == paper.totals["total_operating_expenses"] == 327_000
    assert not_deductible == paper.totals["separated_not_expenses"] == 820_000


# ---------------------------------------------------------------------------
# page boot
# ---------------------------------------------------------------------------


def test_rental_working_paper_page_boots(apptest) -> None:  # type: ignore[no-untyped-def]
    """The read-only rental working-paper page renders against a migrated empty DB."""
    apptest("app_pages/tax_rentals.py")
