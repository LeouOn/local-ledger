"""TAX-06 reconciliation and classification, against the hardened contract.

Each test pins a behaviour a wrong figure would break. The contract is the one
in [tax-06-hardening-proposal.md](../../../docs/tax-filing/tax-06-hardening-proposal.md):

- a classification is bound to a real source row and reads its amount from it;
- one decision head per (workspace, subject, measure) owns the current revision;
- identical replay is a no-op, a changed decision supersedes, history is kept;
- a split validates everything before it writes, and commits once;
- totals are computed over a persisted scope, and every in-scope row is
  accounted for as reviewed, excluded or unresolved;
- a decision whose source has changed is not counted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.ledger.service import LedgerService
from local_ledger.models import Account, Transaction
from local_ledger.tax import (
    DocumentType,
    EvidenceStatus,
    ExtractedField,
    Jurisdiction,
    TaxProperty,
    TaxWorkspaceService,
)
from local_ledger.tax.classification import (
    AUTHORITY_REQUIRED,
    ClassificationError,
    ClassificationState,
    ClassificationSubject,
    ReviewConflictError,
    SourceChangedError,
    TaxClassificationService,
    TaxTreatment,
)
from local_ledger.tax.reconciliation import (
    ReconciliationError,
    ReconciliationService,
    RunState,
    TotalBasis,
    assert_no_model_dependency,
)
from local_ledger.tax.scope import ReviewScopeMode, ScopeError, WorkspaceScopeService
from local_ledger.tax.service import WorkspaceLockedError

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "docs" / "tax-filing" / "fixtures"

_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


@pytest.fixture()
def env(session: Session, engine, tax_env: Path):  # type: ignore[no-untyped-def]
    """A 2025 workspace with one account, a document, and a configured scope."""
    init_db(engine)
    ws_svc = TaxWorkspaceService(session)
    workspace = ws_svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
    doc = ws_svc.register_document(
        workspace_id=workspace.id,
        file_name="synthetic_1099.pdf",
        source=_PDF,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    account = Account(name="Checking", type="checking")
    session.add(account)
    session.commit()
    session.refresh(account)
    return {
        "session": session,
        "ws": ws_svc,
        "workspace": workspace,
        "doc": doc,
        "account": account,
        "cls": TaxClassificationService(session, workspace.id),
        "rec": ReconciliationService(session, workspace.id),
        "scopes": WorkspaceScopeService(session),
    }


@pytest.fixture()
def scoped(env):  # type: ignore[no-untyped-def]
    """``env`` with the review scope configured over the checking account."""
    env["scopes"].configure(
        workspace_id=env["workspace"].id,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[env["account"].id],
        properties_confirmed=True,
    )
    return env


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _tx(session: Session, account, date: str, cents: int, **kw) -> Transaction:  # type: ignore[no-untyped-def]
    tx = Transaction(account_id=account.id, date=date, amount_cents=cents, **kw)
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def _total(report, key: str):  # type: ignore[no-untyped-def]
    for t in report.totals:
        if t.field_key == key:
            return t
    return None


def _verify_expense(env, *, category: str, cents: int, **kw):  # type: ignore[no-untyped-def]
    """A VERIFIED rental expense: classification requires verified source."""
    record = env["ws"].record_rental_expense(
        workspace_id=env["workspace"].id,
        property_id=kw.pop("property_id"),
        category=category,
        amount_cents=cents,
        source_document_id=env["doc"].id,
        **kw,
    )
    env["ws"].verify_rental_expense(record.id)
    return record


# ---------------------------------------------------------------- scope


def test_an_unconfigured_scope_cannot_claim_completeness(env) -> None:  # type: ignore[no-untyped-def]
    """A new workspace knows nothing about what it is reviewing."""
    report = env["rec"].bank_flow_totals()
    assert report.complete is False
    assert report.totals == []
    assert "UNCONFIGURED" in (report.incompleteness_reason or "")


def test_configuring_scope_records_its_members_and_version(scoped) -> None:  # type: ignore[no-untyped-def]
    scope = scoped["scopes"].get(scoped["workspace"].id)
    assert scope.mode == ReviewScopeMode.SELECTED_ACCOUNTS
    assert scope.account_ids == [scoped["account"].id]
    assert scope.version >= 2, "configuring bumps the version"
    assert scope.to_snapshot()["version"] == scope.version


def test_an_empty_account_list_is_not_the_same_as_nothing_found(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ScopeError, match="at least one account"):
        env["scopes"].configure(
            workspace_id=env["workspace"].id,
            mode=ReviewScopeMode.SELECTED_ACCOUNTS,
            account_ids=[],
            property_ids=[],
        )


def test_documents_only_scope_is_a_real_mode(env) -> None:  # type: ignore[no-untyped-def]
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.DOCUMENTS_ONLY
    )
    report = env["rec"].bank_flow_totals()
    assert report.complete is True
    assert report.totals == []
    assert env["rec"].scope().mode == ReviewScopeMode.DOCUMENTS_ONLY


def test_scope_rejects_a_property_from_another_workspace(env) -> None:  # type: ignore[no-untyped-def]
    other = env["ws"].get_or_create_workspace(2024, Jurisdiction.FEDERAL)
    foreign = env["ws"].create_rental_property(
        workspace_id=other.id, street_address="9 B St", city="Fresno",
        state="CA", zip_code="93701",
    )
    with pytest.raises(ScopeError, match="do not belong to workspace"):
        env["scopes"].configure(
            workspace_id=env["workspace"].id,
            mode=ReviewScopeMode.SELECTED_ACCOUNTS,
            account_ids=[env["account"].id],
            property_ids=[foreign.id],
        )


def test_changing_scope_stales_existing_runs(env) -> None:  # type: ignore[no-untyped-def]
    env["scopes"].configure(
        workspace_id=env["workspace"].id,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[env["account"].id],
    )
    run = env["rec"].persist(env["rec"].compute_run(TotalBasis.BANK_FLOW))
    assert run.state == RunState.CURRENT
    version_before = run.scope_version

    env["scopes"].configure(
        workspace_id=env["workspace"].id,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[env["account"].id],
        property_ids=[],
        properties_confirmed=True,
    )

    env["session"].refresh(run)
    assert run.state == RunState.STALE
    # The old run can still explain what it covered.
    assert run.scope_version == version_before
    assert json.loads(run.scope_snapshot_json or "{}")["mode"] == "SELECTED_ACCOUNTS"


# ---------------------------------------------------------------- amount authority


def test_a_classification_reads_its_amount_from_the_source(scoped) -> None:  # type: ignore[no-untyped-def]
    expense = _verify_expense(scoped, property_id=_property(scoped), category="repairs", cents=12345)
    parts = scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE,
        subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_REPAIRS,
        fact="plumbing repair, invoice 4412",
        property_id=expense.property_id,
        authority_citation="2025 Schedule E instructions",
    )
    assert parts[0].amount_cents == 12345
    assert parts[0].source_amount_cents == 12345
    assert parts[0].source_fingerprint


def test_classifying_a_subject_that_does_not_exist_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ClassificationError, match="not found"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE,
            subject_id="exp-does-not-exist",
            treatment=TaxTreatment.RENTAL_SUPPLIES,
            fact="invented",
        )


def test_classifying_unverified_evidence_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    """Verified source amount and reviewed treatment are two different facts."""
    prop = _property(scoped)
    unverified = scoped["ws"].record_rental_expense(
        workspace_id=scoped["workspace"].id, property_id=prop,
        category="insurance", amount_cents=50000, source_document_id=scoped["doc"].id,
    )
    with pytest.raises(ClassificationError, match="verify the source"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE,
            subject_id=unverified.id,
            treatment=TaxTreatment.RENTAL_INSURANCE,
            fact="insurance premium",
        )


def test_a_transaction_outside_the_tax_year_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    prior = _tx(scoped["session"], scoped["account"], "2024-05-05", -1000)
    assert prior.id is not None
    with pytest.raises(ClassificationError, match="outside tax year"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.TRANSACTION,
            subject_id=str(prior.id),
            treatment=TaxTreatment.PERSONAL,
            fact="prior year",
        )


# ---------------------------------------------------------------- replay and history


def test_identical_replay_is_a_no_op(scoped) -> None:  # type: ignore[no-untyped-def]
    """The double-click: re-submitting the same decision changes nothing."""
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="repairs", cents=12345)
    kwargs = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE,
        subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_REPAIRS,
        fact="plumbing repair, invoice 4412",
        property_id=prop,
        authority_citation="2025 Schedule E instructions",
    )
    first = scoped["cls"].classify(**kwargs)
    second = scoped["cls"].classify(**kwargs)

    assert [p.id for p in first] == [p.id for p in second]
    assert len(scoped["cls"].history_for(ClassificationSubject.RENTAL_EXPENSE, expense.id)) == 1
    report = scoped["rec"].rental_totals(prop)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop}").amount_cents == 12345


def test_a_changed_fact_creates_a_new_revision_and_supersedes_the_old(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="repairs", cents=12345)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_REPAIRS, fact="first rationale",
        property_id=prop, authority_citation="2025 Schedule E instructions",
    )
    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_REPAIRS, fact="second, corrected rationale",
        property_id=prop, authority_citation="2025 Schedule E instructions",
    )

    history = scoped["cls"].history_for(ClassificationSubject.RENTAL_EXPENSE, expense.id)
    assert len(history) == 2
    assert {h.state for h in history} == {ClassificationState.CLASSIFIED, ClassificationState.SUPERSEDED}
    # The changed fact must not add a second active amount.
    report = scoped["rec"].rental_totals(prop)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop}").amount_cents == 12345
    # And the prior rationale is still readable.
    assert any("first rationale" in h.fact for h in history)


def test_a_stale_head_version_is_a_conflict_not_an_overwrite(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=5000)
    kwargs = dict(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="supplies", property_id=prop,
    )
    scoped["cls"].classify(**kwargs)
    head = scoped["cls"].head_for(ClassificationSubject.RENTAL_EXPENSE, expense.id)
    stale_version = head.version

    scoped["cls"].classify(**{**kwargs, "fact": "a newer rationale"})

    with pytest.raises(ReviewConflictError, match="stale request"):
        scoped["cls"].classify(
            **{**kwargs, "fact": "an old in-flight rationale"},
            expected_head_version=stale_version,
        )


def test_a_stale_source_fingerprint_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=5000)
    with pytest.raises(SourceChangedError, match="has changed"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment=TaxTreatment.RENTAL_SUPPLIES, fact="supplies", property_id=prop,
            expected_source_fingerprint="not-the-current-fingerprint",
        )


def test_a_source_change_requires_review_again(scoped) -> None:  # type: ignore[no-untyped-def]
    """Reading a new amount must not carry the old approval forward."""
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="insurance", cents=50000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_INSURANCE, fact="premium", property_id=prop,
    )
    before = _total(
        scoped["rec"].rental_totals(prop), f"deductible:{TaxTreatment.RENTAL_INSURANCE}:{prop}"
    )
    assert before.amount_cents == 50000

    # The source amount changes after the approval.
    scoped["session"].expire(expense)
    expense.amount_cents = 75000
    scoped["session"].add(expense)
    scoped["session"].commit()

    after = scoped["rec"].rental_totals(prop)
    assert _total(after, f"deductible:{TaxTreatment.RENTAL_INSURANCE}:{prop}") is None
    assert after.unresolved_count == 1

    moved = scoped["cls"].revalidate()
    assert moved >= 1
    states = {
        h.state
        for h in scoped["cls"].history_for(ClassificationSubject.RENTAL_EXPENSE, expense.id)
    }
    assert ClassificationState.NEEDS_FACT in states


# ---------------------------------------------------------------- splits


def test_a_split_allocates_exactly_and_contributes_once(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    tx = _tx(scoped["session"], scoped["account"], "2025-01-05", -1987200)
    scoped["cls"].split(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        parts=[
            {"treatment": TaxTreatment.RENTAL_MORTGAGE_INTEREST, "allocation_bp": 7321,
             "fact": "1098 box 1 interest", "authority_citation": "2025 Form 1098 box 1"},
            {"treatment": TaxTreatment.RENTAL_PROPERTY_TAX, "allocation_bp": 2679,
             "fact": "county property tax from escrow", "property_id": prop},
        ],
    )
    report = scoped["rec"].bank_flow_totals()
    interest = _total(report, f"income:{TaxTreatment.RENTAL_MORTGAGE_INTEREST}")
    tax = _total(report, f"income:{TaxTreatment.RENTAL_PROPERTY_TAX}")
    assert interest.amount_cents + tax.amount_cents == -1987200


def test_a_split_that_does_not_add_up_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    tx = _tx(scoped["session"], scoped["account"], "2025-01-05", -100000)
    with pytest.raises(ClassificationError, match="10000 basis points"):
        scoped["cls"].split(
            subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
            parts=[
                {"treatment": TaxTreatment.RENTAL_REPAIRS, "allocation_bp": 5000,
                 "fact": "half", "authority_citation": "x"},
                {"treatment": TaxTreatment.RENTAL_SUPPLIES, "allocation_bp": 4000, "fact": "rest"},
            ],
        )


def test_a_failed_split_preserves_the_previous_decision(scoped) -> None:  # type: ignore[no-untyped-def]
    """The P0: a rejected replacement must not have already erased the old one."""
    tx = _tx(scoped["session"], scoped["account"], "2025-01-05", -100000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="original whole decision",
    )
    before = scoped["cls"].current_revision_parts(ClassificationSubject.TRANSACTION, str(tx.id))

    with pytest.raises(ClassificationError):
        scoped["cls"].split(
            subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
            parts=[
                {"treatment": TaxTreatment.RENTAL_REPAIRS, "allocation_bp": 5000,
                 "fact": "first half", "authority_citation": "Pub 527"},
                # missing authority for a legal test -> must abort the whole split
                {"treatment": TaxTreatment.RENTAL_MORTGAGE_INTEREST, "allocation_bp": 5000,
                 "fact": "second half"},
            ],
        )

    after = scoped["cls"].current_revision_parts(ClassificationSubject.TRANSACTION, str(tx.id))
    assert [p.id for p in after] == [p.id for p in before], "prior decision must be intact"
    assert after[0].fact == "original whole decision"
    # And its allocation is not a half-applied zero.
    assert scoped["cls"].allocations_for(ClassificationSubject.TRANSACTION, str(tx.id))[
        after[0].id
    ] == -100000


def test_re_splitting_supersedes_rather_than_accumulates(scoped) -> None:  # type: ignore[no-untyped-def]
    tx = _tx(scoped["session"], scoped["account"], "2025-01-05", -100000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        treatment=TaxTreatment.RENTAL_REPAIRS, fact="first pass",
        authority_citation="Pub 527",
    )
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="second pass, corrected",
    )
    current = scoped["cls"].current_revision_parts(ClassificationSubject.TRANSACTION, str(tx.id))
    assert len(current) == 1
    assert current[0].treatment == TaxTreatment.RENTAL_SUPPLIES
    report = scoped["rec"].bank_flow_totals()
    assert _total(report, f"income:{TaxTreatment.RENTAL_REPAIRS}") is None
    assert _total(report, f"income:{TaxTreatment.RENTAL_SUPPLIES}").amount_cents == -100000


# ---------------------------------------------------------------- validation


def test_a_classification_without_a_fact_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=100)
    with pytest.raises(ClassificationError, match="recorded fact"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment=TaxTreatment.RENTAL_SUPPLIES, fact="   ", property_id=prop,
        )


def test_a_legal_test_treatment_requires_authority(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="repairs", cents=142000)
    with pytest.raises(ClassificationError, match="authority"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment=TaxTreatment.RENTAL_REPAIRS, fact="plumbing", property_id=prop,
        )


def test_an_unregistered_treatment_is_refused(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=100)
    with pytest.raises(ValueError):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment="DEFINITELY_DEDUCTIBLE", fact="looks fine", property_id=prop,
        )


def test_a_locked_workspace_refuses_every_write(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=100)
    assert expense.id is not None
    scoped["ws"].lock_workspace(scoped["workspace"].id)

    with pytest.raises(WorkspaceLockedError, match="LOCKED"):
        scoped["cls"].classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment=TaxTreatment.RENTAL_SUPPLIES, fact="after lock", property_id=prop,
        )
    with pytest.raises(WorkspaceLockedError, match="LOCKED"):
        scoped["cls"].mark_unresolved(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            reason="after lock",
        )
    with pytest.raises(WorkspaceLockedError, match="LOCKED"):
        scoped["scopes"].configure(
            workspace_id=scoped["workspace"].id,
            mode=ReviewScopeMode.SELECTED_ACCOUNTS,
            account_ids=[scoped["account"].id],
        )


# ---------------------------------------------------------------- completeness


def test_unclassified_transactions_are_reported_not_absent(scoped) -> None:  # type: ignore[no-untyped-def]
    """The P0: a row nobody classified used to vanish with no warning."""
    _tx(scoped["session"], scoped["account"], "2025-05-05", -8240)
    report = scoped["rec"].bank_flow_totals()
    assert report.totals == []
    assert report.unresolved_count == 1
    assert report.unresolved_cents == 8240


def test_reviewed_income_and_excluded_flows_land_in_separate_buckets(scoped) -> None:  # type: ignore[no-untyped-def]
    rent = _tx(scoped["session"], scoped["account"], "2025-01-02", 3600000)
    refund = _tx(scoped["session"], scoped["account"], "2025-06-10", 45000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(rent.id),
        treatment=TaxTreatment.RENTAL_GROSS_RENT, fact="rent per annual statement",
    )
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(refund.id),
        treatment=TaxTreatment.REFUND, fact="refund of the 2025-02-14 purchase",
    )
    report = scoped["rec"].bank_flow_totals()
    assert _total(report, f"income:{TaxTreatment.RENTAL_GROSS_RENT}").amount_cents == 3600000
    assert _total(report, f"excluded:{TaxTreatment.REFUND}").amount_cents == 45000
    assert report.unresolved_count == 0


def test_a_paired_transfer_is_never_income(scoped) -> None:  # type: ignore[no-untyped-def]
    savings = Account(name="Savings", type="savings")
    scoped["session"].add(savings)
    scoped["session"].commit()
    scoped["session"].refresh(savings)
    scoped["scopes"].configure(
        workspace_id=scoped["workspace"].id,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[scoped["account"].id, savings.id],
    )
    ledger = LedgerService(scoped["session"])
    out_tx, in_tx = ledger.create_transfer(
        from_account_id=scoped["account"].id, to_account_id=savings.id,
        amount_cents=500000, date="2025-03-31",
    )
    for tx in (out_tx, in_tx):
        proposal = scoped["cls"].propose_for_transaction(tx)
        assert proposal.state == ClassificationState.CLASSIFIED
        scoped["cls"].classify(
            subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
            treatment=proposal.treatment, fact=proposal.reason,
        )
    report = scoped["rec"].bank_flow_totals()
    assert _total(report, f"excluded:{TaxTreatment.TRANSFER_OUT}").amount_cents == -500000
    assert all(not t.field_key.startswith("income:TRANSFER") for t in report.totals)


def test_years_never_mix(scoped) -> None:  # type: ignore[no-untyped-def]
    in_year = _tx(scoped["session"], scoped["account"], "2025-04-15", 2500000)
    _prior = _tx(scoped["session"], scoped["account"], "2024-04-15", 9999000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(in_year.id),
        treatment=TaxTreatment.RENTAL_GROSS_RENT, fact="2025 rent",
    )
    report = scoped["rec"].bank_flow_totals()
    total = _total(report, f"income:{TaxTreatment.RENTAL_GROSS_RENT}")
    assert total.amount_cents == 2500000
    assert report.unresolved_count == 0


# ---------------------------------------------------------------- source form


def test_only_verified_fields_reach_a_source_form_total(scoped) -> None:  # type: ignore[no-untyped-def]
    svc, doc = scoped["ws"], scoped["doc"]
    ws = scoped["workspace"].id
    verified = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="box1a_total_ordinary_dividends",
        raw_text="$3,850.00", amount_cents=385000,
    )
    svc.verify_field(verified.id, verified_by="auditor@local")
    proposed = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="box2a_total_capital_gain_distr",
        raw_text="$450.00", amount_cents=45000,
    )
    rejected = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="box3_nondividend_distributions",
        raw_text="$0.00", amount_cents=0,
    )
    svc.reject_field(rejected.id, rejected_by="auditor@local", notes="not reported")

    report = scoped["rec"].source_form_totals()
    assert _total(report, "box1a_total_ordinary_dividends").amount_cents == 385000
    assert _total(report, "box2a_total_capital_gain_distr").amount_cents == 0
    assert proposed.id in _total(report, "box2a_total_capital_gain_distr").excluded_ids
    assert report.unresolved_count == 1  # Rejected history is not a review obligation.
    assert report.unresolved_cents == 45000


def test_a_corrected_document_drops_the_old_amount_and_stales_the_run(scoped) -> None:  # type: ignore[no-untyped-def]
    svc, doc, ws = scoped["ws"], scoped["doc"], scoped["workspace"].id
    field = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="box1a_total_ordinary_dividends",
        raw_text="$3,850.00", amount_cents=385000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    run = scoped["rec"].persist(scoped["rec"].compute_run(TotalBasis.SOURCE_FORM))
    assert run.state == RunState.CURRENT

    svc.register_document(
        workspace_id=ws, file_name="corrected.pdf", source=b"%PDF-1.4 corrected rev2",
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
        is_corrected=True, supersedes_doc_id=doc.id,
    )

    scoped["session"].refresh(run)
    assert run.state == RunState.STALE, "a correction must stale the persisted run"
    report = scoped["rec"].source_form_totals()
    assert _total(report, "box1a_total_ordinary_dividends").amount_cents == 0
    assert field.id in _total(report, "box1a_total_ordinary_dividends").excluded_ids


def test_a_superseded_amount_cannot_come_back(scoped) -> None:  # type: ignore[no-untyped-def]
    from local_ledger.tax.service import InvalidStateTransitionError

    svc, doc, ws = scoped["ws"], scoped["doc"], scoped["workspace"].id
    field = svc.propose_field(
        workspace_id=ws, document_id=doc.id, field_name="proceeds",
        raw_text="$12,500.00", amount_cents=1250000,
    )
    svc.verify_field(field.id, verified_by="auditor@local")
    svc.register_document(
        workspace_id=ws, file_name="corrected.pdf", source=b"%PDF-1.4 corrected rev3",
        doc_type=DocumentType.FORM_1099_CONSOLIDATED, is_corrected=True, supersedes_doc_id=doc.id,
    )
    with pytest.raises(InvalidStateTransitionError):
        svc.verify_field(field.id, verified_by="auditor@local")
    assert _total(scoped["rec"].source_form_totals(), "proceeds").amount_cents == 0


# ---------------------------------------------------------------- traceability and determinism


def test_a_total_names_the_rows_that_produced_it(scoped) -> None:  # type: ignore[no-untyped-def]
    for cents in (1250000, 420000):
        field = scoped["ws"].propose_field(
            workspace_id=scoped["workspace"].id, document_id=scoped["doc"].id,
            field_name="proceeds", raw_text=f"${cents / 100:,.2f}", amount_cents=cents,
        )
        scoped["ws"].verify_field(field.id, verified_by="auditor@local")
    total = _total(scoped["rec"].source_form_totals(), "proceeds")
    contributing = {
        f.id
        for f in scoped["session"]
        .exec(select(ExtractedField).where(ExtractedField.field_name == "proceeds"))
        .all()
    }
    assert total.amount_cents == 1670000
    assert set(total.member_ids) == contributing


def test_runs_are_reproducible_and_hash_stable(scoped) -> None:  # type: ignore[no-untyped-def]
    field = scoped["ws"].propose_field(
        workspace_id=scoped["workspace"].id, document_id=scoped["doc"].id,
        field_name="box1_interest_income", raw_text="$1,425.50", amount_cents=142550,
    )
    scoped["ws"].verify_field(field.id, verified_by="auditor@local")
    rec = scoped["rec"]
    first, second = rec.compute_run(TotalBasis.SOURCE_FORM), rec.compute_run(TotalBasis.SOURCE_FORM)
    assert first.content_hash == second.content_hash
    assert rec.persist(first).content_hash == rec.persist(second).content_hash


def test_a_changed_scope_or_evidence_changes_the_hash(scoped) -> None:  # type: ignore[no-untyped-def]
    field = scoped["ws"].propose_field(
        workspace_id=scoped["workspace"].id, document_id=scoped["doc"].id,
        field_name="proceeds", raw_text="$1.00", amount_cents=100,
    )
    scoped["ws"].verify_field(field.id, verified_by="auditor@local")
    before = scoped["rec"].compute_run(TotalBasis.SOURCE_FORM).content_hash
    extra = scoped["ws"].propose_field(
        workspace_id=scoped["workspace"].id, document_id=scoped["doc"].id,
        field_name="proceeds", raw_text="$2.00", amount_cents=200,
    )
    scoped["ws"].verify_field(extra.id, verified_by="auditor@local")
    assert scoped["rec"].compute_run(TotalBasis.SOURCE_FORM).content_hash != before


def test_ledger_edits_stale_a_run(scoped) -> None:  # type: ignore[no-untyped-def]
    tx = _tx(scoped["session"], scoped["account"], "2025-01-02", 3600000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.TRANSACTION, subject_id=str(tx.id),
        treatment=TaxTreatment.RENTAL_GROSS_RENT, fact="rent",
    )
    run = scoped["rec"].persist(scoped["rec"].compute_run(TotalBasis.BANK_FLOW))
    assert run.state == RunState.CURRENT

    LedgerService(scoped["session"]).delete_transaction(tx.id, actor="user")

    scoped["session"].refresh(run)
    assert run.state == RunState.STALE


def test_a_new_in_scope_transaction_stales_a_previous_run(scoped) -> None:  # type: ignore[no-untyped-def]
    """A run that said 'complete' must not stay complete once scope grows."""
    run = scoped["rec"].persist(scoped["rec"].compute_run(TotalBasis.BANK_FLOW))
    assert run.complete is True

    LedgerService(scoped["session"]).create_transaction(Transaction(
        account_id=scoped["account"].id, date="2025-09-09", amount_cents=-50000,
    ))
    scoped["session"].refresh(run)
    assert run.state == RunState.STALE


def test_changing_a_decision_stales_the_run_built_from_it(scoped) -> None:  # type: ignore[no-untyped-def]
    """A classification change is an evidence change, like a correction is."""
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=8000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="first review", property_id=prop,
    )
    run = scoped["rec"].persist(scoped["rec"].compute_run(TotalBasis.SOURCE_FORM))
    assert run.state == RunState.CURRENT

    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="second review", property_id=prop,
    )

    scoped["session"].refresh(run)
    assert run.state == RunState.STALE, (
        "a run must not stay current after the decision beneath it was replaced"
    )


def test_marking_unresolved_stales_a_run(scoped) -> None:  # type: ignore[no-untyped-def]
    prop = _property(scoped)
    expense = _verify_expense(scoped, property_id=prop, category="supplies", cents=8000)
    scoped["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.RENTAL_SUPPLIES, fact="first", property_id=prop,
    )
    run = scoped["rec"].persist(scoped["rec"].compute_run(TotalBasis.SOURCE_FORM))
    assert run.state == RunState.CURRENT

    scoped["cls"].mark_unresolved(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        reason="the evidence for this line turned out to be a duplicate",
    )

    scoped["session"].refresh(run)
    assert run.state == RunState.STALE


# ---------------------------------------------------------------- investments


def test_investment_measures_stay_distinct(env) -> None:  # type: ignore[no-untyped-def]
    """Proceeds cannot supply basis, and missing basis stays unknown."""
    fixture = _fixture("brokerage-1099-consolidated.json")
    from local_ledger.tax.classification import Measure

    for lot in fixture["transactions_1099_b"]:
        sale = env["ws"].record_investment_sale(
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
        env["ws"].verify_investment_sale(sale.id)
        env["cls"].classify(
            subject_type=ClassificationSubject.INVESTMENT_SALE,
            subject_id=sale.id,
            measure=Measure.PROCEEDS,
            treatment=TaxTreatment.INVESTMENT_PROCEEDS,
            fact=f"proceeds per the 1099-B, {lot['locator']['box_or_line']}",
        )
        if not lot.get("is_basis_missing"):
            env["cls"].classify(
                subject_type=ClassificationSubject.INVESTMENT_SALE,
                subject_id=sale.id,
                measure=Measure.BASIS,
                treatment=TaxTreatment.INVESTMENT_BASIS,
                fact=f"basis per the 1099-B, {lot['locator']['box_or_line']}",
            )

    report = env["rec"].investment_totals()
    totals = {t.field_key: t for t in report.totals}
    long_lots = [lot for lot in fixture["transactions_1099_b"] if lot["term"] == "LONG_TERM"]
    short_lots = [lot for lot in fixture["transactions_1099_b"] if lot["term"] == "SHORT_TERM"]
    assert totals["proceeds_long_term"].amount_cents == sum(
        lot["proceeds_cents"] for lot in long_lots
    )
    assert totals["proceeds_short_term"].amount_cents == sum(
        lot["proceeds_cents"] for lot in short_lots
    )
    assert totals["basis_reported_long_term"].amount_cents == sum(
        lot["cost_basis_cents"] for lot in long_lots if lot.get("cost_basis_cents")
    )
    assert totals["wash_sale_disallowed"].amount_cents == 32000
    # The noncovered lot: proceeds known, basis unknown - not filled in.
    assert report.unresolved_count == 1
    assert report.unresolved_cents == 310000
    assert len(totals["basis_reported_long_term"].member_ids) == sum(
        1 for lot in long_lots if lot.get("cost_basis_cents")
    )


def test_basis_measure_cannot_be_classified_on_a_lot_without_one(env) -> None:  # type: ignore[no-untyped-def]
    from local_ledger.tax.classification import Measure

    sale = env["ws"].record_investment_sale(
        workspace_id=env["workspace"].id, source_document_id=env["doc"].id,
        security_description="NONCOVERED", date_sold="2025-09-18", proceeds_cents=310000,
        cost_basis_cents=None, gain_loss_cents=None, term="LONG_TERM",
        basis_reported_to_irs=False,
    )
    env["ws"].verify_investment_sale(sale.id)
    with pytest.raises(ClassificationError, match="no value for measure"):
        env["cls"].classify(
            subject_type=ClassificationSubject.INVESTMENT_SALE, subject_id=sale.id,
            measure=Measure.BASIS, treatment=TaxTreatment.INVESTMENT_BASIS, fact="basis",
        )


# ---------------------------------------------------------------- rentals, fixture-driven


def test_rental_fixture_reconciles_to_the_statement(env) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture("rental-statement-property-a.json")
    ws_svc, cls = env["ws"], env["cls"]
    p = fixture["property"]
    prop = ws_svc.create_rental_property(
        workspace_id=env["workspace"].id,
        street_address=p["street_address"], city=p["city"], state=p["state"],
        zip_code=p["zip_code"], property_type=p["property_type"],
        ownership_basis_points=p["ownership_basis_points"], days_rented=p["days_rented"],
        date_placed_in_service=p["date_placed_in_service"],
    )
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.DOCUMENTS_ONLY,
        property_ids=[prop.id], properties_confirmed=True,
    )
    for name, cents in fixture["operating_expenses"].items():
        if cents == 0:
            continue
        expense = ws_svc.record_rental_expense(
            workspace_id=env["workspace"].id, property_id=prop.id, category=name,
            amount_cents=cents, source_document_id=env["doc"].id,
            treatment_authority="2025 Schedule E instructions",
        )
        ws_svc.verify_rental_expense(expense.id)
        result = cls.propose_for_rental_expense(expense)
        assert result.treatment is not None, f"{name} should map to a treatment"
        cls.classify(
            subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
            treatment=result.treatment, fact=f"{name} per the annual statement",
            property_id=prop.id,
            authority_citation=(
                expense.treatment_authority if result.treatment in AUTHORITY_REQUIRED else None
            ),
        )

    report = env["rec"].rental_totals(prop.id)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_MORTGAGE_INTEREST}:{prop.id}").amount_cents == 1245000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_PROPERTY_TAX}:{prop.id}").amount_cents == 560000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_INSURANCE}:{prop.id}").amount_cents == 185000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop.id}").amount_cents == 142000
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_HOA_DUES}:{prop.id}").amount_cents == 240000


def test_a_capital_improvement_suspect_never_lands_in_expenses(env) -> None:  # type: ignore[no-untyped-def]
    fixture = _fixture("rental-statement-property-a.json")
    ws_svc, cls = env["ws"], env["cls"]
    prop = ws_svc.create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.DOCUMENTS_ONLY,
        property_ids=[prop.id], properties_confirmed=True,
    )
    suspect = fixture["capital_improvement_suspects"][0]
    expense = ws_svc.record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id,
        category=suspect["categorized_as"], amount_cents=suspect["amount_cents"],
        source_document_id=env["doc"].id, treatment_authority=suspect["authority"],
        is_capital_improvement_suspect=True,
    )
    ws_svc.verify_rental_expense(expense.id)
    result = cls.propose_for_rental_expense(expense)
    assert result.treatment == TaxTreatment.CAPITAL_IMPROVEMENT
    assert result.state == ClassificationState.NEEDS_FACT
    cls.classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=result.treatment,
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
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.DOCUMENTS_ONLY,
        property_ids=[prop.id], properties_confirmed=True,
    )
    expense = env["ws"].record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id, category="principal",
        amount_cents=principal, source_document_id=env["doc"].id,
    )
    env["ws"].verify_rental_expense(expense.id)
    env["cls"].classify(
        subject_type=ClassificationSubject.RENTAL_EXPENSE, subject_id=expense.id,
        treatment=TaxTreatment.MORTGAGE_PRINCIPAL,
        fact=fixture["mortgage_statement_1098_reconciliation"]["principal_note"],
        property_id=prop.id,
        authority_citation="2025 Schedule E (Form 540) instructions, line 12",
    )
    report = env["rec"].rental_totals(prop.id)
    assert _total(
        report, f"not_deductible:{TaxTreatment.MORTGAGE_PRINCIPAL}:{prop.id}"
    ).amount_cents == principal
    assert _total(report, f"deductible:{TaxTreatment.MORTGAGE_PRINCIPAL}:{prop.id}") is None


def test_an_undecided_expense_appears_separately(env) -> None:  # type: ignore[no-untyped-def]
    prop = env["ws"].create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.DOCUMENTS_ONLY,
        property_ids=[prop.id], properties_confirmed=True,
    )
    expense = env["ws"].record_rental_expense(
        workspace_id=env["workspace"].id, property_id=prop.id,
        category="strange_misc_charge", amount_cents=33333, source_document_id=env["doc"].id,
    )
    env["ws"].verify_rental_expense(expense.id)
    result = env["cls"].propose_for_rental_expense(expense)
    assert result.treatment is None
    report = env["rec"].rental_totals(prop.id)
    assert _total(report, f"deductible:{TaxTreatment.RENTAL_REPAIRS}:{prop.id}") is None
    assert report.unresolved_cents == 33333


# ---------------------------------------------------------------- guards


def test_reconciliation_has_no_model_dependency() -> None:
    assert_no_model_dependency("local_ledger.tax.reconciliation")
    assert_no_model_dependency("local_ledger.tax.classification")


def test_bad_input_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReconciliationError, match="workspace"):
        ReconciliationService(env["session"], "ws-nope")
    with pytest.raises(ReconciliationError, match="unknown basis"):
        env["rec"].compute_run("SOMETHING_ELSE")
    with pytest.raises(ReconciliationError):
        env["rec"].rental_totals("prop-not-here")


def test_evidence_status_is_the_only_thing_that_gates_a_source_total(scoped) -> None:  # type: ignore[no-untyped-def]
    svc, ws, doc = scoped["ws"], scoped["workspace"].id, scoped["doc"].id
    for name, state in (("interest", "verify"), ("dividends", "reject"), ("gain_dist", "none")):
        field = svc.propose_field(
            workspace_id=ws, document_id=doc, field_name=name,
            raw_text="$1.00", amount_cents=100,
        )
        if state == "verify":
            svc.verify_field(field.id, verified_by="auditor@local")
        elif state == "reject":
            svc.reject_field(field.id, rejected_by="auditor@local", notes="n/a")
    report = scoped["rec"].source_form_totals()
    assert _total(report, "interest").amount_cents == 100
    assert _total(report, "dividends").amount_cents == 0
    assert _total(report, "gain_dist").amount_cents == 0
    assert EvidenceStatus.VERIFIED == "VERIFIED"


def _property(env) -> str:  # type: ignore[no-untyped-def]
    """One rental property in the workspace, created once per test."""
    existing = list(
        env["session"]
        .exec(select(TaxProperty).where(TaxProperty.workspace_id == env["workspace"].id))
        .all()
    )
    if existing:
        return existing[0].id
    created = env["ws"].create_rental_property(
        workspace_id=env["workspace"].id, street_address="123 Citrus Lane",
        city="Pasadena", state="CA", zip_code="91101",
    )
    env["scopes"].configure(
        workspace_id=env["workspace"].id, mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        account_ids=[env["account"].id], property_ids=[created.id], properties_confirmed=True,
    )
    return created.id
