"""TAX-06: annual reconciliation and tax classification.

The numbers here come from the synthetic fixtures in
``docs/tax-filing/fixtures/``, so these tests are the first place the broker and
rental fixtures are actually reconciled. Each test pins one behaviour the tax
audit depends on:

- source-form totals and bank-flow totals stay apart, and a transfer is never
  income;
- a total carries the ids of the rows that produced it;
- unverified and undecided amounts are counted separately, never folded into a
  total that then looks complete;
- a split allocation adds up exactly, and cannot contribute twice in full;
- a capital-improvement suspect never lands in an expense bucket;
- a ledger edit makes a computed run stale;
- no model is involved in any of the arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select
from tests.factories import make_account

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Transaction
from local_ledger.tax import (
    DocumentType,
    EvidenceStatus,
    ExtractedField,
    Jurisdiction,
    TaxWorkspaceService,
)
from local_ledger.tax.classification import (
    AUTHORITY_REQUIRED,
    ClassificationError,
    ClassificationState,
    ClassificationSubject,
    TaxClassificationService,
    TaxTreatment,
)
from local_ledger.tax.reconciliation import (
    ReconciliationError,
    ReconciliationRun,
    ReconciliationService,
    RunState,
    TotalBasis,
    assert_no_model_dependency,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "docs" / "tax-filing" / "fixtures"


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def env(session: Session, engine, tax_env: Path):  # type: ignore[no-untyped-def]
    """A 2025 federal workspace, a document, and a service pair to work with."""
    init_db(engine)
    ws_svc = TaxWorkspaceService(session)
    workspace = ws_svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    pdf = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )
    doc = ws_svc.register_document(
        workspace_id=workspace.id,
        file_name="synthetic_1099.pdf",
        source=pdf,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    return {
        "session": session,
        "ws": ws_svc,
        "workspace": workspace,
        "doc": doc,
        "cls": TaxClassificationService(session, workspace.id),
        "rec": ReconciliationService(session, workspace.id),
    }


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _tx(session: Session, account_id: int, date: str, cents: int, **kw) -> Transaction:
    tx = Transaction(account_id=account_id, date=date, amount_cents=cents, **kw)
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _total(report, key: str):  # type: ignore[no-untyped-def]
    for t in report.totals:
        if t.field_key == key:
            return t
    return None


# ---------------------------------------------------------------- classification


def test_a_paired_transfer_is_never_income(env) -> None:  # type: ignore[no-untyped-def]
    """Structure alone decides this one: a paired transfer row is not income."""
    session = env["session"]
    acc = make_account(session, name="Checking")
    other = make_account(session, name="Savings")
    session.commit()
    ledger = LedgerService(session)
    out_tx, in_tx = ledger.create_transfer(
        from_account_id=acc.id, to_account_id=other.id, amount_cents=500000, date="2025-03-31"
    )
    cls = env["cls"]

    for tx in (out_tx, in_tx):
        result = cls.propose_for_transaction(tx)
        assert result.state == ClassificationState.CLASSIFIED
        record = cls.classify(
            subject_type=ClassificationSubject.TRANSACTION,
            subject_id=str(tx.id),
            amount_cents=tx.amount_cents,
            treatment=result.treatment,
            fact=result.reason,
        )
        assert record.treatment in {TaxTreatment.TRANSFER_OUT, TaxTreatment.TRANSFER_IN}

    report = env["rec"].bank_flow_totals(account_ids=[acc.id, other.id])
    assert _total(report, f"excluded:{TaxTreatment.TRANSFER_OUT}").amount_cents == -500000
    assert all(not t.field_key.startswith("income:") for t in report.totals), (
        "a transfer must not produce an income total"
    )


def test_an_unmatched_transaction_stays_unresolved_not_silently_income(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-05-05", -8240)

    result = env["cls"].propose_for_transaction(tx)
    assert result.treatment is None
    env["cls"].mark_unresolved(
        subject_type=ClassificationSubject.TRANSACTION,
        subject_id=str(tx.id),
        amount_cents=tx.amount_cents,
        reason=result.reason,
    )

    report = env["rec"].bank_flow_totals(account_ids=[acc.id])
    assert report.totals == [], "an undecided amount must not create a total"
    assert report.unresolved_count == 1
    assert report.unresolved_cents == 8240


def test_a_refund_is_excluded_from_income_once_reviewed(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-06-10", 45000)
    env["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION,
        subject_id=str(tx.id),
        amount_cents=45000,
        treatment=TaxTreatment.REFUND,
        fact="refund of the 2025-02-14 hardware purchase, invoice 8821",
    )

    report = env["rec"].bank_flow_totals(account_ids=[acc.id])
    assert _total(report, f"excluded:{TaxTreatment.REFUND}").amount_cents == 45000
    assert all(not t.field_key.startswith("income:") for t in report.totals)


def test_rent_income_is_classified_and_totalled(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-01-02", 3600000)
    env["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION,
        subject_id=str(tx.id),
        amount_cents=3600000,
        treatment=TaxTreatment.RENTAL_GROSS_RENT,
        fact=f"rent received per the annual statement, {env['doc'].id} box 'Gross Rents'",
    )

    report = env["rec"].bank_flow_totals(account_ids=[acc.id])
    assert _total(report, f"income:{TaxTreatment.RENTAL_GROSS_RENT}").amount_cents == 3600000


def test_a_classification_without_a_fact_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ClassificationError, match="recorded fact"):
        env["cls"].classify(
            subject_type=ClassificationSubject.TRANSACTION,
            subject_id="1",
            amount_cents=100,
            treatment=TaxTreatment.RENTAL_REPAIRS,
            fact="   ",
        )


def test_a_legal_test_treatment_requires_authority(env) -> None:  # type: ignore[no-untyped-def]
    """Repairs vs improvement is a legal test, so it cannot be recorded bare."""
    with pytest.raises(ClassificationError, match="authority"):
        env["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE,
            subject_id="exp-1",
            amount_cents=142000,
            treatment=TaxTreatment.RENTAL_REPAIRS,
            fact="plumbing repair, invoice 4412",
        )

    record = env["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE,
        subject_id="exp-1",
        amount_cents=142000,
        treatment=TaxTreatment.RENTAL_REPAIRS,
        fact="plumbing repair, invoice 4412; no betterment to the structure",
        authority_citation="2025 Form 1040 Schedule E (Form 540) instructions, repairs vs improvements",
    )
    assert record.authority_citation


def test_an_unregistered_treatment_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        env["cls"].classify(
            subject_type=ClassificationSubject.TRANSACTION,
            subject_id="1",
            amount_cents=100,
            treatment="DEFINITELY_DEDUCTIBLE",
            fact="looks fine to me",
        )


# ---------------------------------------------------------------- splits


def test_a_split_allocates_exactly_and_contributes_once(env) -> None:  # type: ignore[no-untyped-def]
    """One ledger row, two treatments: the shares must add up with no gap."""
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-01-05", -1987200)
    prop = env["ws"].create_rental_property(
        workspace_id=env["workspace"].id,
        street_address="123 Citrus Lane",
        city="Pasadena",
        state="CA",
        zip_code="91101",
    )
    env["cls"].split(
        subject_type=ClassificationSubject.TRANSACTION,
        subject_id=str(tx.id),
        total_cents=-1987200,
        parts=[
            {
                "treatment": TaxTreatment.RENTAL_MORTGAGE_INTEREST,
                "amount_cents": 0,
                "allocation_bp": 7321,
                "fact": "1098 box 1 interest for 2025",
                "authority_citation": "2025 Form 1098 box 1",
            },
            {
                "treatment": TaxTreatment.RENTAL_PROPERTY_TAX,
                "amount_cents": 0,
                "allocation_bp": 2679,
                "fact": "county property tax paid from escrow",
                "property_id": prop.id,
            },
        ],
    )
    rows = env["cls"].for_subject(ClassificationSubject.TRANSACTION, str(tx.id))
    assert len(rows) == 2
    assert sum(env["cls"].allocation_cents(r) for r in rows) == -1987200
    assert sum(r.allocation_bp for r in rows) == 10000


def test_a_split_that_does_not_add_up_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-01-05", -100000)
    with pytest.raises(ClassificationError, match="10000 basis points"):
        env["cls"].split(
            subject_type=ClassificationSubject.TRANSACTION,
            subject_id=str(tx.id),
            total_cents=-100000,
            parts=[
                {"treatment": TaxTreatment.RENTAL_REPAIRS, "amount_cents": 0,
                 "allocation_bp": 5000, "fact": "half", "authority_citation": "x"},
                {"treatment": TaxTreatment.RENTAL_SUPPLIES, "amount_cents": 0,
                 "allocation_bp": 4000, "fact": "other half"},
            ],
        )


def test_re_splitting_replaces_the_previous_split(env) -> None:  # type: ignore[no-untyped-def]
    """Re-classifying must not leave the old shares behind to double count."""
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-01-05", -100000)
    parts = [
        {"treatment": TaxTreatment.RENTAL_REPAIRS, "amount_cents": 0, "allocation_bp": 10000,
         "fact": "first pass", "authority_citation": "x"},
    ]
    env["cls"].split(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        total_cents=-100000, parts=parts,
    )
    env["cls"].split(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        total_cents=-100000,
        parts=[
            {"treatment": TaxTreatment.RENTAL_SUPPLIES, "amount_cents": 0,
             "allocation_bp": 10000, "fact": "second pass, corrected category"},
        ],
    )
    rows = env["cls"].for_subject(ClassificationSubject.TRANSACTION, str(tx.id))
    assert len(rows) == 1
    assert rows[0].treatment == TaxTreatment.RENTAL_SUPPLIES


# ---------------------------------------------------------------- source-form totals


def test_only_verified_fields_reach_a_source_form_total(env) -> None:  # type: ignore[no-untyped-def]
    svc, doc = env["ws"], env["doc"]
    verified = svc.propose_field(
        workspace_id=env["workspace"].id, document_id=doc.id,
        field_name="box1a_total_ordinary_dividends", raw_text="$3,850.00", amount_cents=385000,
    )
    svc.verify_field(verified.id, verified_by="auditor@local")
    proposed = svc.propose_field(
        workspace_id=env["workspace"].id, document_id=doc.id,
        field_name="box2a_total_capital_gain_distr", raw_text="$450.00", amount_cents=45000,
    )
    rejected = svc.propose_field(
        workspace_id=env["workspace"].id, document_id=doc.id,
        field_name="box3_nondividend_distributions", raw_text="$0.00", amount_cents=0,
    )
    svc.reject_field(rejected.id, rejected_by="auditor@local", notes="not reported on this form")

    report = env["rec"].source_form_totals()
    assert _total(report, "box1a_total_ordinary_dividends").amount_cents == 385000
    assert _total(report, "box2a_total_capital_gain_distr").amount_cents == 0
    assert proposed.id in _total(report, "box2a_total_capital_gain_distr").excluded_ids
    assert report.unresolved_count == 2
    assert report.unresolved_cents == 45000


def test_a_corrected_document_removes_the_old_amount_from_the_total(env) -> None:  # type: ignore[no-untyped-def]
    svc, doc = env["ws"], env["doc"]
    ws = env["workspace"].id
    field = svc.propose_field(
        workspace_id=ws, document_id=doc.id,
        field_name="box1a_total_ordinary_dividends", raw_text="$3,850.00", amount_cents=385000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    assert _total(env["rec"].source_form_totals(), "box1a_total_ordinary_dividends").amount_cents == 385000

    svc.register_document(
        workspace_id=ws, file_name="corrected.pdf",
        source=b"%PDF-1.4 corrected", doc_type=DocumentType.FORM_1099_CONSOLIDATED,
        is_corrected=True, supersedes_doc_id=doc.id,
    )

    report = env["rec"].source_form_totals()
    assert _total(report, "box1a_total_ordinary_dividends").amount_cents == 0
    assert field.id in _total(report, "box1a_total_ordinary_dividends").excluded_ids


def test_a_superseded_amount_cannot_come_back(env) -> None:  # type: ignore[no-untyped-def]
    from local_ledger.tax.service import InvalidStateTransitionError

    svc, doc = env["ws"], env["doc"]
    ws = env["workspace"].id
    field = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="proceeds",
        raw_text="$12,500.00", amount_cents=1250000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    svc.register_document(
        workspace_id=ws, file_name="corrected.pdf", source=b"%PDF-1.4 corrected",
        doc_type=DocumentType.FORM_1099_CONSOLIDATED, is_corrected=True, supersedes_doc_id=doc.id,
    )
    with pytest.raises(InvalidStateTransitionError):
        svc.verify_field(field.id, verified_by="auditor@local")
    assert _total(env["rec"].source_form_totals(), "proceeds").amount_cents == 0


# ---------------------------------------------------------------- traceability


def test_a_total_names_the_rows_that_produced_it(env) -> None:  # type: ignore[no-untyped-def]
    svc, doc = env["ws"], env["doc"]
    ws = env["workspace"].id
    for cents in (1250000, 420000):
        field = svc.propose_field(
            workspace_id=ws, document_id=doc.id, field_name="proceeds",
            raw_text=f"${cents / 100:,.2f}", amount_cents=cents,
        )
        svc.verify_field(field.id, verified_by="auditor@local")

    total = _total(env["rec"].source_form_totals(), "proceeds")
    assert total.amount_cents == 1670000
    contributing = {
        f.id
        for f in env["session"]
        .exec(select(ExtractedField).where(ExtractedField.field_name == "proceeds"))
        .all()
    }
    assert len(total.member_ids) == 2
    assert set(total.member_ids) == contributing


def test_a_run_persists_its_totals_and_members(env) -> None:  # type: ignore[no-untyped-def]
    field = env["ws"].propose_field(
        workspace_id=env["workspace"].id, document_id=env["doc"].id,
        field_name="box1_interest_income", raw_text="$1,425.50", amount_cents=142550,
    )
    env["ws"].verify_field(field.id, verified_by="auditor@local")

    report = env["rec"].compute_run(TotalBasis.SOURCE_FORM)
    run = env["rec"].persist(report)

    assert run.basis == TotalBasis.SOURCE_FORM
    assert run.tax_year == 2025
    assert run.state == RunState.CURRENT
    totals = {t.field_key: t for t in env["rec"].totals_of(run)}
    assert totals["box1_interest_income"].amount_cents == 142550
    assert totals["box1_interest_income"].member_ids == (field.id,)


def test_totals_are_reproducible_and_hash_stable(env) -> None:  # type: ignore[no-untyped-def]
    field = env["ws"].propose_field(
        workspace_id=env["workspace"].id, document_id=env["doc"].id,
        field_name="box1_interest_income", raw_text="$1,425.50", amount_cents=142550,
    )
    env["ws"].verify_field(field.id, verified_by="auditor@local")
    rec = env["rec"]

    first = rec.compute_run(TotalBasis.SOURCE_FORM)
    second = rec.compute_run(TotalBasis.SOURCE_FORM)
    assert first.content_hash == second.content_hash
    assert [t.amount_cents for t in first.totals] == [t.amount_cents for t in second.totals]

    run_a, run_b = rec.persist(first), rec.persist(second)
    assert run_a.content_hash == run_b.content_hash


def test_a_changed_evidence_set_changes_the_hash(env) -> None:  # type: ignore[no-untyped-def]
    field = env["ws"].propose_field(
        workspace_id=env["workspace"].id, document_id=env["doc"].id,
        field_name="proceeds", raw_text="$1.00", amount_cents=100,
    )
    env["ws"].verify_field(field.id, verified_by="auditor@local")
    before = env["rec"].compute_run(TotalBasis.SOURCE_FORM).content_hash

    extra = env["ws"].propose_field(
        workspace_id=env["workspace"].id, document_id=env["doc"].id,
        field_name="proceeds", raw_text="$2.00", amount_cents=200,
    )
    env["ws"].verify_field(extra.id, verified_by="auditor@local")

    assert env["rec"].compute_run(TotalBasis.SOURCE_FORM).content_hash != before


def test_ledger_edits_make_a_computed_run_stale(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    tx = _tx(session, acc.id, "2025-01-02", 3600000)
    env["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        amount_cents=3600000, treatment=TaxTreatment.RENTAL_GROSS_RENT,
        fact="rent per annual statement",
    )
    rec = env["rec"]
    run = rec.persist(rec.compute_run(TotalBasis.BANK_FLOW))
    assert run.state == RunState.CURRENT

    LedgerService(session).delete_transaction(tx.id, actor="user")

    session.refresh(run)
    assert run.state == RunState.STALE, "a run must not stay current after its evidence is deleted"


# ---------------------------------------------------------------- year isolation


def test_totals_never_include_another_tax_year(env) -> None:  # type: ignore[no-untyped-def]
    session = env["session"]
    acc = make_account(session)
    session.commit()
    in_year = _tx(session, acc.id, "2025-04-15", 2500000)
    prior_year = _tx(session, acc.id, "2024-04-15", 9999000)
    for tx in (in_year, prior_year):
        env["cls"].classify(
            subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
            amount_cents=tx.amount_cents, treatment=TaxTreatment.RENTAL_GROSS_RENT,
            fact="synthetic rent",
        )

    report = env["rec"].bank_flow_totals(account_ids=[acc.id])
    total = _total(report, f"income:{TaxTreatment.RENTAL_GROSS_RENT}")
    assert total.amount_cents == 2500000
    assert total.member_ids == (
        env["cls"].for_subject(ClassificationSubject.TRANSACTION, str(in_year.id))[0].id,
    )


# ---------------------------------------------------------------- rental working papers


def test_rental_fixture_reconciles_to_the_statement(env) -> None:  # type: ignore[no-untyped-def]
    """The prop-001 fixture: gross rent in, expenses bucketed by treatment."""
    fixture = _fixture("rental-statement-property-a.json")
    ws, doc, cls = env["ws"], env["doc"], env["cls"]
    p = fixture["property"]
    prop = ws.create_rental_property(
        workspace_id=env["workspace"].id,
        street_address=p["street_address"], city=p["city"], state=p["state"],
        zip_code=p["zip_code"], property_type=p["property_type"],
        ownership_basis_points=p["ownership_basis_points"],
        days_rented=p["days_rented"],
        date_placed_in_service=p["date_placed_in_service"],
    )
    ws_svc = env["ws"]
    for name, cents in fixture["operating_expenses"].items():
        if cents == 0:
            continue
        expense = ws_svc.record_rental_expense(
            workspace_id=env["workspace"].id, property_id=prop.id,
            category=name, amount_cents=cents,
            source_document_id=doc.id,
            treatment_authority="2025 Schedule E instructions",
        )
        result = cls.propose_for_rental_expense(expense)
        assert result.treatment is not None, f"{name} should map to a treatment"
        cls.classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE,
            subject_id=expense.id, amount_cents=cents, treatment=result.treatment,
            fact=f"{name} per the annual statement, box {name}",
            property_id=prop.id,
            authority_citation=(
                expense.treatment_authority
                if result.treatment in AUTHORITY_REQUIRED
                else None
            ),
        )

    rent_account = make_account(env["session"], name="Rental Operating")
    env["session"].commit()
    rent_tx = _tx(env["session"], rent_account.id, "2025-06-30", fixture["gross_rents_received_cents"])
    env["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(rent_tx.id),
        amount_cents=fixture["gross_rents_received_cents"],
        treatment=TaxTreatment.RENTAL_GROSS_RENT,
        fact=f"gross rents received per the {fixture['document_id']} statement",
        property_id=prop.id,
    )

    report = env["rec"].rental_totals(prop.id)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_MORTGAGE_INTEREST}:{prop.id}").amount_cents == 1245000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_PROPERTY_TAX}:{prop.id}").amount_cents == 560000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_INSURANCE}:{prop.id}").amount_cents == 185000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop.id}").amount_cents == 142000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_HOA_DUES}:{prop.id}").amount_cents == 240000


def test_a_capital_improvement_suspect_never_lands_in_expenses(env) -> None:  # type: ignore[no-untyped-def]
    """The HVAC replacement is the fixture's deliberate trap."""
    fixture = _fixture("rental-statement-property-a.json")
    ws_svc, cls = env["ws"], env["cls"]
    prop = ws_svc.create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    suspect = fixture["capital_improvement_suspects"][0]
    expense = ws_svc.record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id,
        category=suspect["categorized_as"], amount_cents=suspect["amount_cents"],
        source_document_id=env["doc"].id,
        treatment_authority=suspect["authority"],
        is_capital_improvement_suspect=True,
    )

    result = cls.propose_for_rental_expense(expense)
    assert result.treatment == TaxTreatment.CAPITAL_IMPROVEMENT
    assert result.state == ClassificationState.NEEDS_FACT, (
        "a suspected improvement needs a human decision, not an auto-classification"
    )
    cls.classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        amount_cents=suspect["amount_cents"], treatment=result.treatment,
        fact=f"{suspect['description']} on {suspect['date']} extends asset life",
        property_id=prop.id, authority_citation=suspect["authority"],
    )

    report = env["rec"].rental_totals(prop.id)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop.id}") is None
    assert _total(
        report, f"not_deductible:{TaxTreatment.CAPITAL_IMPROVEMENT}:{prop.id}"
    ).amount_cents == suspect["amount_cents"]


def test_mortgage_principal_is_tracked_as_not_an_expense(env) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture("rental-statement-property-a.json")
    principal = fixture["mortgage_statement_1098_reconciliation"]["principal_paid_current_year_cents"]
    prop = env["ws"].create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    expense = env["ws"].record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id,
        category="principal", amount_cents=principal, source_document_id=env["doc"].id,
    )
    env["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        amount_cents=principal, treatment=TaxTreatment.MORTGAGE_PRINCIPAL,
        fact=fixture["mortgage_statement_1098_reconciliation"]["principal_note"],
        property_id=prop.id,
        authority_citation="2025 Schedule E (Form 540) instructions, line 12",
    )
    report = env["rec"].rental_totals(prop.id)
    assert _total(
        report, f"not_deductible:{TaxTreatment.MORTGAGE_PRINCIPAL}:{prop.id}"
    ).amount_cents == principal
    assert _total(report, f"deductible:{TaxTreatment.MORTGAGE_PRINCIPAL}:{prop.id}") is None


def test_an_undecided_expense_appears_separately_not_in_a_total(env) -> None:  # type: ignore[no-untyped-def]
    prop = env["ws"].create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    expense = env["ws"].record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id,
        category="strange_misc_charge", amount_cents=33333, source_document_id=env["doc"].id,
    )
    result = env["cls"].propose_for_rental_expense(expense)
    assert result.treatment is None
    env["cls"].mark_unresolved(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        amount_cents=33333, reason=result.reason, property_id=prop.id,
    )

    report = env["rec"].rental_totals(prop.id)
    assert report.unresolved_cents == 33333
    assert _total(report, f"unresolved:{prop.id}").amount_cents == 33333
    assert not any(t.field_key.startswith("deductible:") for t in report.totals)


# ---------------------------------------------------------------- investments


def test_brokerage_fixture_keeps_unreported_basis_out_of_the_total(env) -> None:  # type: ignore[no-untyped-def]
    """lot-003 has no reported basis; the total must not pretend it is zero."""
    fixture = _fixture("brokerage-1099-consolidated.json")
    ws_svc = env["ws"]
    lots = fixture["transactions_1099_b"]
    for lot in lots:
        sale = ws_svc.record_investment_sale(
            workspace_id=env["workspace"].id,
            source_document_id=env["doc"].id,
            security_description=lot["description"],
            cusip_or_ticker=lot["cusip"],
            date_acquired=lot["date_acquired"],
            date_sold=lot["date_sold"],
            proceeds_cents=lot["proceeds_cents"],
            cost_basis_cents=None if lot.get("is_basis_missing") else lot["cost_basis_cents"],
            gain_loss_cents=None if lot.get("is_basis_missing") else lot["gain_loss_cents"],
            term=lot["term"],
            basis_reported_to_irs=lot["basis_reported_to_irs"],
            wash_sale_disallowed_cents=lot.get("wash_sale_disallowed_cents", 0),
            raw_text=lot["locator"]["snippet_text"],
        )
        ws_svc.verify_investment_sale(sale.id)
    # Lot-level treatment questions are TAX-13; this task owns the totals.

    report = env["rec"].investment_totals()
    lt = [lot for lot in lots if lot["term"] == "LONG_TERM"]
    st = [lot for lot in lots if lot["term"] == "SHORT_TERM"]
    assert _total(report, "proceeds_long_term").amount_cents == sum(
        lot["proceeds_cents"] for lot in lt
    )
    assert _total(report, "proceeds_short_term").amount_cents == sum(
        lot["proceeds_cents"] for lot in st
    )
    assert _total(report, "basis_reported_long_term").amount_cents == sum(
        lot["cost_basis_cents"] for lot in lt if lot.get("cost_basis_cents")
    )
    assert _total(report, "wash_sale_disallowed").amount_cents == 32000
    # The noncovered lot's basis is absent, so it is counted as unresolved
    # rather than contributing a zero that looks like a real figure.
    assert report.unresolved_count == 1
    assert report.unresolved_cents == 310000


def test_investment_totals_name_the_lots_behind_them(env) -> None:  # type: ignore[no-untyped-def]
    """A capital-gain figure nobody can take apart is not reviewable."""
    fixture = _fixture("brokerage-1099-consolidated.json")
    ws_svc = env["ws"]
    for lot in fixture["transactions_1099_b"]:
        sale = ws_svc.record_investment_sale(
            workspace_id=env["workspace"].id,
            source_document_id=env["doc"].id,
            security_description=lot["description"],
            cusip_or_ticker=lot["cusip"],
            date_acquired=lot["date_acquired"],
            date_sold=lot["date_sold"],
            proceeds_cents=lot["proceeds_cents"],
            cost_basis_cents=None if lot.get("is_basis_missing") else lot["cost_basis_cents"],
            gain_loss_cents=None if lot.get("is_basis_missing") else lot["gain_loss_cents"],
            term=lot["term"],
            basis_reported_to_irs=lot["basis_reported_to_irs"],
            wash_sale_disallowed_cents=lot.get("wash_sale_disallowed_cents", 0),
            raw_text=lot["locator"]["snippet_text"],
        )
        ws_svc.verify_investment_sale(sale.id)

    report = env["rec"].investment_totals()
    totals = {t.field_key: t for t in report.totals}
    long_lots = [lot for lot in fixture["transactions_1099_b"] if lot["term"] == "LONG_TERM"]
    short_lots = [lot for lot in fixture["transactions_1099_b"] if lot["term"] == "SHORT_TERM"]
    # Every total names the sale rows that produced it, and the wash-sale total
    # names only the lot that actually reported one.
    assert len(totals["proceeds_long_term"].member_ids) == len(long_lots)
    assert len(totals["proceeds_short_term"].member_ids) == len(short_lots)
    assert len(totals["wash_sale_disallowed"].member_ids) == 1
    # Basis totals name only the lots whose basis was actually reported.
    assert len(totals["basis_reported_long_term"].member_ids) == sum(
        1 for lot in long_lots if lot.get("cost_basis_cents")
    )


# ---------------------------------------------------------------- determinism guard


def test_reconciliation_has_no_model_dependency() -> None:
    assert_no_model_dependency("local_ledger.tax.reconciliation")
    assert_no_model_dependency("local_ledger.tax.classification")


def test_an_unknown_basis_or_run_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReconciliationError, match="workspace"):
        ReconciliationService(env["session"], "ws-nope")
    with pytest.raises(ReconciliationError, match="unknown basis"):
        env["rec"].compute_run("SOMETHING_ELSE")
    with pytest.raises(ReconciliationError):
        env["rec"].rental_totals("prop-not-here")


def test_run_rows_are_typed_and_auditable(env) -> None:  # type: ignore[no-untyped-def]
    """The persisted shape is stable enough for a later export to depend on."""
    run = env["rec"].persist(env["rec"].compute_run(TotalBasis.SOURCE_FORM))
    assert isinstance(run, ReconciliationRun)
    assert run.id.startswith("run-")
    assert run.tax_year == 2025
    assert run.unresolved_cents >= 0
    assert json.loads(run.totals_json) == []
    assert env["rec"].current_runs() == [run]
    assert env["rec"].mark_stale() == 1
    session = env["session"]
    session.refresh(run)
    assert run.state == RunState.STALE
    assert env["rec"].current_runs() == []


def test_evidence_status_is_the_only_thing_that_gates_a_total(env) -> None:  # type: ignore[no-untyped-def]
    """Guard the invariant directly: three fields, three states, one total."""
    ws_svc = env["ws"]
    ids = {}
    for name, state in (
        ("interest", "verify"),
        ("dividends", "reject"),
        ("capital_gain_dist", "none"),
    ):
        field = ws_svc.propose_field(
            workspace_id=env["workspace"].id, document_id=env["doc"].id,
            field_name=name, raw_text="$1.00", amount_cents=100,
        )
        if state == "verify":
            ws_svc.verify_field(field.id, verified_by="auditor@local")
        elif state == "reject":
            ws_svc.reject_field(field.id, rejected_by="auditor@local", notes="n/a")
        ids[name] = field.id

    report = env["rec"].source_form_totals()
    assert _total(report, "interest").amount_cents == 100
    assert _total(report, "dividends").amount_cents == 0
    assert _total(report, "capital_gain_dist").amount_cents == 0
    assert _total(report, "dividends").excluded_ids == (ids["dividends"],)
    assert EvidenceStatus.VERIFIED == "VERIFIED"
