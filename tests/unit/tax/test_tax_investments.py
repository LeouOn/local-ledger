"""Investment working papers (TAX-13): audit inputs, exact comparisons, cited questions.

Acceptance scenarios, each from the task:

- synthetic corrected 1099: superseded statement's evidence is excluded, the
  correction's lineage is stated;
- missing basis: unknown, not zero - proceeds known, basis unresolved, gain
  never invented;
- summary/detail duplicate: a summary line equal to a detail lot is flagged,
  and a summary that does not cross-foot its lots is flagged;
- reported wash-sale adjustment: preserved, net-after-wash shown beside the
  gross figure, replacement-purchase review surfaced as a question;
- dividends and capital-loss carryover: exact comparisons, and a missing
  prior return is a question, not a zero;
- gains never derive from bank withdrawals/deposits: adding large ledger
  flows changes nothing, because the working paper never reads them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session
from tests.factories import make_account, make_transaction

from local_ledger.db import init_db
from local_ledger.tax import Jurisdiction, TaxWorkspaceService
from local_ledger.tax.investments import (
    DISCREPANCY,
    MATCHED,
    NOT_RECORDED,
    ORPHANED,
    SOURCE_DRIFT,
    InvestmentAuditService,
)
from local_ledger.tax.models import JurisdictionCarryover
from local_ledger.tax.reconciliation import ReconciliationService, TotalBasis


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


def _doc(svc: TaxWorkspaceService, ws: str, name: str, rev: bytes = b"v1") -> object:
    return svc.register_document(
        workspace_id=ws,
        file_name=name,
        source=b"%PDF-1.4 synthetic-brokerage-" + rev,
        doc_type="1099_CONSOLIDATED",
    )


def _field(svc: TaxWorkspaceService, ws: str, doc_id: str, name: str, cents: int, verify: bool = True):
    f = svc.propose_field(
        workspace_id=ws,
        document_id=doc_id,
        field_name=name,
        raw_text=f"${cents / 100:,.2f}",
        amount_cents=cents,
        page_number=2,
        box_or_line=name,
    )
    if verify:
        svc.verify_field(f.id, verified_by="reviewer")
    return f


def _sale(svc: TaxWorkspaceService, ws: str, doc_id: str, **kwargs):
    defaults = dict(
        workspace_id=ws,
        source_document_id=doc_id,
        security_description="SYNTH LOT",
        date_sold="2025-06-20",
        proceeds_cents=100_000,
        cost_basis_cents=80_000,
        gain_loss_cents=20_000,
        term="LONG_TERM",
        raw_text="synthetic lot line",
    )
    defaults.update(kwargs)
    sale = svc.record_investment_sale(**defaults)
    svc.verify_investment_sale(sale.id)
    return sale


def _questions(paper) -> dict[str, list]:  # type: ignore[no-untyped-def]
    by_code: dict[str, list] = {}
    for q in paper.questions:
        by_code.setdefault(q.code, []).append(q)
    return by_code


# ---------------------------------------------------------------------------
# the synthetic consolidated 1099 scenario
# ---------------------------------------------------------------------------


def test_working_paper_from_synthetic_consolidated_1099(svc: TaxWorkspaceService, ws: str) -> None:
    """The fixture scenario, with exact working-paper totals."""
    doc = _doc(svc, ws, "brokerage_1099_consol.pdf")
    # 1099-INT section (both fixture-style and short alias names must map)
    _field(svc, ws, doc.id, "box1_interest_income", 142_550)
    _field(svc, ws, doc.id, "box3_us_savings_bonds", 25_000)
    # 1099-DIV section
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 385_000)
    _field(svc, ws, doc.id, "box1b_qualified_dividends", 290_000)
    _field(svc, ws, doc.id, "box2a_total_capital_gain_distr", 45_000)
    _field(svc, ws, doc.id, "box7_foreign_tax_paid", 8_500)
    # 1099-B lots: covered LT gain, covered ST wash loss, noncovered unknown basis
    _sale(
        svc, ws, doc.id,
        security_description="ACME CORP (100 SHS)", cusip_or_ticker="001234101",
        date_acquired="2024-03-15", date_sold="2025-06-20",
        proceeds_cents=1_250_000, cost_basis_cents=980_000, gain_loss_cents=270_000,
        term="LONG_TERM",
    )
    _sale(
        svc, ws, doc.id,
        security_description="BETA TECH (50 SHS)", cusip_or_ticker="005678202",
        date_acquired="2025-01-10", date_sold="2025-04-12",
        proceeds_cents=420_000, cost_basis_cents=510_000, gain_loss_cents=-90_000,
        term="SHORT_TERM", wash_sale_disallowed_cents=32_000,
    )
    _sale(
        svc, ws, doc.id,
        security_description="GAMMA HOLDINGS (200 SHS)", cusip_or_ticker="009999303",
        date_acquired=None, date_sold="2025-09-18",
        proceeds_cents=310_000, cost_basis_cents=None, gain_loss_cents=None,
        term="LONG_TERM", basis_reported_to_irs=False,
    )

    paper = InvestmentAuditService(svc.session, ws).working_paper()

    assert paper.totals["interest.box1"] == 142_550
    assert paper.totals["interest.box3"] == 25_000
    assert paper.totals["dividends.box1a"] == 385_000
    assert paper.totals["dividends.box1b"] == 290_000
    assert paper.totals["dividends.box2a"] == 45_000
    assert paper.totals["dividends.box7"] == 8_500

    assert paper.totals["proceeds_short_term"] == 420_000
    assert paper.totals["proceeds_long_term"] == 1_560_000  # known- and unknown-basis lots
    assert paper.totals["basis_reported_short_term"] == 510_000
    assert paper.totals["basis_reported_long_term"] == 980_000  # GAMMA excluded: unknown
    assert paper.totals["wash_sale_disallowed"] == 32_000
    assert paper.totals["gain_reported_short_term"] == -90_000
    assert paper.totals["net_gain_short_term"] == -58_000  # wash adds back to the loss
    assert paper.totals["gain_reported_long_term"] == 270_000  # GAMMA's gain is NOT invented
    assert paper.totals["net_gain_long_term"] == 270_000

    # Unknown basis: unresolved, never zero, cited.
    assert paper.unresolved_basis_proceeds_cents == 310_000
    assert paper.unresolved_basis_lot_count == 1
    questions = _questions(paper)
    assert len(questions["UNKNOWN_BASIS"]) == 1
    assert "UNKNOWN, not zero" in questions["UNKNOWN_BASIS"][0].message
    assert "GAMMA" in questions["UNKNOWN_BASIS"][0].message
    assert "brokerage_1099_consol.pdf" in questions["UNKNOWN_BASIS"][0].citation

    # Wash sale: preserved and questioned, with the replacement lot review
    # explicitly the reviewer's.
    assert len(questions["WASH_SALE_REVIEW"]) == 1
    assert "replacement" in questions["WASH_SALE_REVIEW"][0].message.lower()

    # Income lines carry citations back to the statement page/box.
    line = next(ln for ln in paper.income_lines if ln.key == "interest.box1")
    assert "p.2" in line.citation and "box1_interest_income" in line.citation


def test_totals_agree_with_reconciliation_measures(svc: TaxWorkspaceService, ws: str) -> None:
    """The paper's sale totals equal the TAX-06 investment measures, by name.

    Reconciliation counts sale measures through reviewed classification
    decisions (amounts read from the source), so the comparison needs those
    decisions to exist - exactly the integration this test pins.
    """
    from local_ledger.tax.classification import (
        ClassificationSubject,
        Measure,
        TaxClassificationService,
        TaxTreatment,
    )

    doc = _doc(svc, ws, "consol.pdf", rev=b"a")
    lot_l = _sale(svc, ws, doc.id, proceeds_cents=200_000, cost_basis_cents=150_000, gain_loss_cents=50_000, term="LONG_TERM")
    lot_s = _sale(svc, ws, doc.id, proceeds_cents=90_000, cost_basis_cents=100_000, gain_loss_cents=-10_000, term="SHORT_TERM")

    cls = TaxClassificationService(svc.session, ws)
    for lot in (lot_l, lot_s):
        cls.classify(
            subject_type=ClassificationSubject.INVESTMENT_SALE,
            subject_id=lot.id,
            measure=Measure.PROCEEDS,
            treatment=TaxTreatment.INVESTMENT_PROCEEDS,
            fact="proceeds per the synthetic 1099-B detail",
        )
        cls.classify(
            subject_type=ClassificationSubject.INVESTMENT_SALE,
            subject_id=lot.id,
            measure=Measure.BASIS,
            treatment=TaxTreatment.INVESTMENT_BASIS,
            fact="basis per the synthetic 1099-B detail",
        )

    paper = InvestmentAuditService(svc.session, ws).working_paper()
    report = ReconciliationService(svc.session, ws).investment_totals()
    by_key = {t.field_key: t.amount_cents for t in report.totals}

    assert paper.totals["proceeds_short_term"] == by_key["proceeds_short_term"]
    assert paper.totals["proceeds_long_term"] == by_key["proceeds_long_term"]
    assert paper.totals["basis_reported_short_term"] == by_key["basis_reported_short_term"]
    assert paper.totals["basis_reported_long_term"] == by_key["basis_reported_long_term"]
    assert paper.totals["wash_sale_disallowed"] == by_key["wash_sale_disallowed"]
    assert report.basis == TotalBasis.SOURCE_FORM


# ---------------------------------------------------------------------------
# statement sanity checks
# ---------------------------------------------------------------------------


def test_reported_gain_must_equal_proceeds_minus_basis(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "consol.pdf", rev=b"b")
    _sale(svc, ws, doc.id, proceeds_cents=200_000, cost_basis_cents=100_000, gain_loss_cents=50_000)
    paper = InvestmentAuditService(svc.session, ws).working_paper()
    (question,) = _questions(paper)["ARITHMETIC_MISMATCH"]
    # Amounts render in dollars; inputs are integer cents.
    assert "$500.00" in question.message
    assert "$2,000.00" in question.message
    assert "$1,000.00" in question.message


def test_qualified_dividends_cannot_exceed_ordinary(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "div.pdf", rev=b"c")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 100_000)
    _field(svc, ws, doc.id, "box1b_qualified_dividends", 150_000)
    _field(svc, ws, doc.id, "box2a_total_capital_gain_distr", 200_000)
    paper = InvestmentAuditService(svc.session, ws).working_paper()
    codes = _questions(paper)
    assert codes["QUALIFIED_EXCEEDS_ORDINARY"][0].citation.startswith("div.pdf")
    assert "box 1b cannot exceed box 1a" in codes["QUALIFIED_EXCEEDS_ORDINARY"][0].message
    assert codes["CAPITAL_DISTR_EXCEEDS_ORDINARY"]


def test_summary_detail_duplicate_is_flagged(svc: TaxWorkspaceService, ws: str) -> None:
    """A summary line equal to one lot's proceeds is a duplicate-reporting question."""
    doc = _doc(svc, ws, "b.pdf", rev=b"d")
    _field(svc, ws, doc.id, "1099b_gross_proceeds", 420_000)
    _sale(svc, ws, doc.id, security_description="WHOLE ACCOUNT LIQUIDATION", proceeds_cents=420_000, cost_basis_cents=100_000, gain_loss_cents=320_000)
    paper = InvestmentAuditService(svc.session, ws).working_paper()
    (question,) = _questions(paper)["DUPLICATE_SUMMARY_DETAIL"]
    assert "summary" in question.message and "Enter the figure once" in question.message


def test_summary_that_does_not_crossfoot_is_flagged(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "b.pdf", rev=b"e")
    _field(svc, ws, doc.id, "1099b_gross_proceeds", 500_000)
    _sale(svc, ws, doc.id, proceeds_cents=200_000, cost_basis_cents=150_000, gain_loss_cents=50_000)
    _sale(svc, ws, doc.id, proceeds_cents=220_000, cost_basis_cents=180_000, gain_loss_cents=40_000, date_sold="2025-07-01")
    paper = InvestmentAuditService(svc.session, ws).working_paper()
    (question,) = _questions(paper)["SUMMARY_CROSSFOOT"]
    assert "$5,000.00" in question.message and "$4,200.00" in question.message


def test_identical_lot_on_two_statements_is_a_cross_account_question(svc: TaxWorkspaceService, ws: str) -> None:
    doc_a = _doc(svc, ws, "broker_a.pdf", rev=b"f1")
    doc_b = _doc(svc, ws, "broker_b.pdf", rev=b"f2")
    for doc in (doc_a, doc_b):
        _sale(
            svc, ws, doc.id,
            security_description="SAME CORP (10 SHS)", cusip_or_ticker="777777101",
            date_sold="2025-03-03", proceeds_cents=333_000, cost_basis_cents=300_000,
            gain_loss_cents=33_000, term="SHORT_TERM",
        )
    paper = InvestmentAuditService(svc.session, ws).working_paper()
    (question,) = _questions(paper)["DUPLICATE_CROSS_DOCUMENT"]
    assert "two different statements" in question.message
    assert "broker_a.pdf" in question.citation and "broker_b.pdf" in question.citation


# ---------------------------------------------------------------------------
# corrected statement lineage
# ---------------------------------------------------------------------------


def test_corrected_statement_excludes_superseded_evidence(svc: TaxWorkspaceService, ws: str) -> None:
    v1 = _doc(svc, ws, "1099_v1.pdf", rev=b"g1")
    _field(svc, ws, v1.id, "box1a_total_ordinary_dividends", 100_000)
    _sale(svc, ws, v1.id, proceeds_cents=900_000, cost_basis_cents=800_000, gain_loss_cents=100_000, term="LONG_TERM")

    v2 = svc.register_document(
        workspace_id=ws,
        file_name="1099_v2_corrected.pdf",
        source=b"%PDF-1.4 synthetic-brokerage-g2",
        doc_type="1099_CONSOLIDATED",
        is_corrected=True,
        supersedes_doc_id=v1.id,
    )
    _field(svc, ws, v2.id, "box1a_total_ordinary_dividends", 385_000)
    _sale(svc, ws, v2.id, proceeds_cents=1_250_000, cost_basis_cents=980_000, gain_loss_cents=270_000, term="LONG_TERM")

    paper = InvestmentAuditService(svc.session, ws).working_paper()

    # Only the correction's evidence counts.
    assert paper.totals["dividends.box1a"] == 385_000
    assert paper.totals["proceeds_long_term"] == 1_250_000
    assert all(s.document_id == v2.id for s in paper.sales)
    assert all(ln.document_id == v2.id for ln in paper.income_lines)

    # The lineage is stated, not silent.
    assert any("1099_v2_corrected.pdf supersedes 1099_v1.pdf" in n for n in paper.correction_notes)
    assert paper.correction_notes[0].endswith("excluded from the working paper.")


# ---------------------------------------------------------------------------
# gains never derive from bank flows
# ---------------------------------------------------------------------------


def test_gains_never_derive_from_bank_flows(svc: TaxWorkspaceService, ws: str, session: Session) -> None:
    doc = _doc(svc, ws, "consol.pdf", rev=b"h")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 50_000)
    _sale(svc, ws, doc.id, proceeds_cents=100_000, cost_basis_cents=90_000, gain_loss_cents=10_000)

    audit = InvestmentAuditService(session, ws)
    before = audit.working_paper()

    # Large deposits and withdrawals that dwarf the investment figures: a
    # bank flow is not income and must not move a single working-paper cent.
    account = make_account(session, name="Brokerage Checking")
    make_transaction(session, account_id=account.id, amount_cents=50_000_000, date="2025-06-01")
    make_transaction(session, account_id=account.id, amount_cents=-30_000_000, date="2025-08-01")

    after = audit.working_paper()
    assert after.totals == before.totals
    assert {(q.code, q.message) for q in after.questions} == {
        (q.code, q.message) for q in before.questions
    }
    assert after.totals["net_gain_long_term"] == 10_000


def test_module_reads_no_bank_flow_tables() -> None:
    """Structural guarantee: investments.py imports neither ledger rows nor classifications."""
    import ast
    import inspect

    from local_ledger.tax import investments

    tree = ast.parse(inspect.getsource(investments))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    assert not any("models" in name for name in imported if "tax" not in name), sorted(imported)
    assert not any(
        name for name in imported if name in {"Transaction", "ClassificationSubject", "TaxClassification"}
    ), sorted(imported)


# ---------------------------------------------------------------------------
# carryovers
# ---------------------------------------------------------------------------


def test_missing_prior_return_is_a_question_not_zero(svc: TaxWorkspaceService, ws: str) -> None:
    session = svc.session
    session.add(
        JurisdictionCarryover(
            workspace_id=ws,
            jurisdiction=Jurisdiction.FEDERAL,
            category="CAPITAL_LOSS",
            prior_year_cents=None,
            prior_return_reference="",
        )
    )
    session.commit()

    paper = InvestmentAuditService(session, ws).working_paper()
    (question,) = _questions(paper)["MISSING_PRIOR_RETURN"]
    assert "UNKNOWN" in question.message
    assert "missing information, not zero" in question.message
    assert paper.carryovers[0].prior_year_cents is None


def test_recorded_carryover_appears_as_context(svc: TaxWorkspaceService, ws: str) -> None:
    session = svc.session
    session.add(
        JurisdictionCarryover(
            workspace_id=ws,
            jurisdiction=Jurisdiction.FEDERAL,
            category="CAPITAL_LOSS",
            prior_year_cents=150_000,
            utilized_current_year_cents=30_000,
            remaining_carryover_cents=120_000,
            prior_return_reference="2024 Form 1040 Schedule D line 16",
        )
    )
    session.commit()

    paper = InvestmentAuditService(session, ws).working_paper()
    (line,) = paper.carryovers
    assert line.prior_year_cents == 150_000
    assert line.remaining_carryover_cents == 120_000
    assert line.prior_return_reference == "2024 Form 1040 Schedule D line 16"
    assert not any(q.code == "MISSING_PRIOR_RETURN" for q in paper.questions)


# ---------------------------------------------------------------------------
# TaxAct comparisons
# ---------------------------------------------------------------------------


def _paper_with_one_lot(svc: TaxWorkspaceService, ws: str):  # type: ignore[no-untyped-def]
    doc = _doc(svc, ws, "consol.pdf", rev=b"i")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 385_000)
    _sale(svc, ws, doc.id, proceeds_cents=100_000, cost_basis_cents=90_000, gain_loss_cents=10_000)
    return doc


def test_comparisons_matched_discrepant_and_not_recorded(svc: TaxWorkspaceService, ws: str) -> None:
    _paper_with_one_lot(svc, ws)
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1a",
        source_working_paper_cents=385_000,
        taxact_entered_cents=385_000,
        delta_cents=0,
        status="MATCHED",
    )
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Form 8949",
        field_key="proceeds_long_term",
        source_working_paper_cents=100_000,
        taxact_entered_cents=90_000,  # under-entered by $100
        delta_cents=-10_000,
        status="DISCREPANCY",
    )

    rows = {(r.tax_section, r.field_key): r for r in InvestmentAuditService(svc.session, ws).comparisons()}

    matched = rows[("1099-DIV", "dividends.box1a")]
    assert matched.status == MATCHED and matched.delta_cents == 0

    discrepant = rows[("Form 8949", "proceeds_long_term")]
    assert discrepant.status == DISCREPANCY
    assert discrepant.delta_cents == -10_000
    assert "under-entered" in discrepant.note

    missing = rows[("Schedule D", "gain_reported_long_term")]
    assert missing.status == NOT_RECORDED and missing.taxact_entered_cents is None

    # Unrecorded keys far outnumber recorded ones on a fresh workspace; the
    # statuses are explicit rather than absent.
    assert sum(1 for r in rows.values() if r.status == NOT_RECORDED) > 5


def test_comparison_flags_source_drift(svc: TaxWorkspaceService, ws: str) -> None:
    _paper_with_one_lot(svc, ws)
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1a",
        source_working_paper_cents=385_000,
        taxact_entered_cents=385_000,
        delta_cents=0,
        status="MATCHED",
    )
    # The comparison was staled by the evidence write (integration fix), but
    # if a current comparison ever survives a moved paper, the audit says so:
    # simulate by recording against a future amount that no longer holds.
    item = svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1b",
        source_working_paper_cents=999_999,
        taxact_entered_cents=999_999,
        delta_cents=0,
        status="MATCHED",
    )
    assert item.source_working_paper_cents == 999_999

    rows = {(r.tax_section, r.field_key): r for r in InvestmentAuditService(svc.session, ws).comparisons()}
    drifted = rows[("1099-DIV", "dividends.box1b")]
    # box1b has no evidence at all: expected 0, recorded against 999_999.
    assert drifted.status == SOURCE_DRIFT
    assert drifted.working_paper_cents == 0
    assert "re-review" in drifted.note


def test_orphaned_comparison_key_is_surfaced(svc: TaxWorkspaceService, ws: str) -> None:
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Form 8949",
        field_key="proceeds_toto",  # typo of totals; paper has no such key
        source_working_paper_cents=1,
        taxact_entered_cents=1,
        delta_cents=0,
        status="MATCHED",
    )
    rows = {(r.tax_section, r.field_key): r for r in InvestmentAuditService(svc.session, ws).comparisons()}
    orphan = rows[("Form 8949", "proceeds_toto")]
    assert orphan.status == ORPHANED
    assert orphan.working_paper_cents is None
    assert "no longer produces it" in orphan.note


# ---------------------------------------------------------------------------
# pending evidence stays visible and excluded
# ---------------------------------------------------------------------------


def test_proposed_fields_are_pending_not_counted(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "consol.pdf", rev=b"j")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 385_000, verify=True)
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 400_000, verify=False)  # correction pending
    _field(svc, ws, doc.id, "some_unknown_box", 12_345, verify=False)
    # A verified field the alias table does not know is surfaced, not dropped.
    _field(svc, ws, doc.id, "state_tax_withholding", 7_777, verify=True)

    paper = InvestmentAuditService(svc.session, ws).working_paper()
    assert paper.totals["dividends.box1a"] == 385_000  # verified only
    assert len(paper.pending_income) == 2  # proposed rows: visible, excluded
    assert {p.field_name for p in paper.pending_income} == {
        "box1a_total_ordinary_dividends",
        "some_unknown_box",
    }
    assert {p.field_name for p in paper.unmapped_fields} == {"state_tax_withholding"}
    assert paper.unmapped_fields[0].amount_cents == 7_777


# ---------------------------------------------------------------------------
# page boot
# ---------------------------------------------------------------------------


def test_investment_working_paper_page_boots(apptest) -> None:  # type: ignore[no-untyped-def]
    """The read-only working-paper page renders against a migrated empty DB."""
    apptest("app_pages/tax_investments.py")
