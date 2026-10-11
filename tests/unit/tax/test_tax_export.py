"""TaxAct field coverage and export (TAX-08).

Acceptance scenarios from the task, each pinned:

- coverage proves every *required* TaxAct field has a current, matched
  comparison against today's amounts (the stronger gate the TAX-06 review
  asked for); zero-evidence fields are not required;
- deterministic export reproduces the reviewed totals and sources;
- drafts cannot masquerade as approved packets (labelled everywhere; final
  refused with named gaps);
- a deliberately incorrect TaxAct entry, missing basis and missing rental
  support produce focused blockers;
- resolved items retain their explanation and evidence (history via the
  workspace service);
- formula-like text is inert in CSV output;
- every approved amount traces to its evidence citations;
- changed evidence marks an approved export stale;
- no raw documents or private paths in the export.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session

# Reuse the audit-track seeding helpers' shape.
from tests.unit.tax.test_tax_investments import _doc, _field, _sale
from tests.unit.tax.test_tax_rentals import _exp, _prop

from local_ledger.db import init_db
from local_ledger.tax import Jurisdiction, TaxWorkspaceService, taxact_fields
from local_ledger.tax.export import (
    CSV_NAME,
    MANIFEST_NAME,
    PACKET_NAME,
    ExportReadinessError,
    TaxExportError,
    export_packet,
    verify_export_current,
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


def _seed_investments(svc: TaxWorkspaceService, ws: str) -> None:
    doc = _doc(svc, ws, "brokerage_1099.pdf", b"t8a")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 385_000)
    _sale(
        svc, ws, doc.id,
        security_description="ACME CORP (100 SHS)", cusip_or_ticker="001234101",
        date_acquired="2024-03-15", date_sold="2025-06-20",
        proceeds_cents=1_250_000, cost_basis_cents=980_000, gain_loss_cents=270_000,
        term="LONG_TERM",
    )


REQUIRED_INVESTMENT_KEYS = {
    ("1099-DIV", "dividends.box1a"),
    ("Form 8949", "proceeds_long_term"),
    ("Form 8949", "basis_reported_long_term"),
    ("Schedule D", "gain_reported_long_term"),
    ("Schedule D", "net_gain_long_term"),
}


def _record_matched_for_required(svc: TaxWorkspaceService, ws: str) -> None:
    coverage = taxact_fields.coverage_report(svc.session, ws)
    for row in coverage.rows:
        if row.verdict == taxact_fields.NOT_RECORDED and row.required:
            svc.record_reconciliation_item(
                workspace_id=ws,
                tax_section=row.tax_section,
                field_key=row.field_key,
                source_working_paper_cents=row.working_paper_cents or 0,
                taxact_entered_cents=row.working_paper_cents or 0,
                delta_cents=0,
                status="MATCHED",
                explanation="Reviewed against the working paper.",
            )


def _configure_scope(session: Session, ws: str) -> None:
    WorkspaceScopeService(session).configure(
        workspace_id=ws,
        mode=ReviewScopeMode.SELECTED_ACCOUNTS,
        properties_confirmed=True,
    )


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


def test_coverage_gaps_until_every_required_field_is_matched(
    svc: TaxWorkspaceService, ws: str
) -> None:
    _seed_investments(svc, ws)
    coverage = taxact_fields.coverage_report(svc.session, ws)

    required = {r.verdict for r in coverage.rows if r.required}
    assert taxact_fields.NOT_RECORDED in required
    assert coverage.complete is False

    by_key = {(r.tax_section, r.field_key): r for r in coverage.rows}
    assert (("1099-DIV", "dividends.box1a") in by_key)
    # Zero-evidence fields are not required: nobody records 0 = 0 ceremony.
    assert by_key[("Form 8949", "proceeds_short_term")].verdict == taxact_fields.NOT_REQUIRED

    _record_matched_for_required(svc, ws)
    coverage = taxact_fields.coverage_report(svc.session, ws)
    assert coverage.complete is True
    assert all(
        r.verdict in (taxact_fields.COVERED, taxact_fields.NOT_REQUIRED)
        for r in coverage.rows
    )


def test_wrong_taxact_entry_is_a_focused_discrepancy(svc: TaxWorkspaceService, ws: str) -> None:
    _seed_investments(svc, ws)
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1a",
        source_working_paper_cents=385_000,
        taxact_entered_cents=385_100,  # $1 over-entered
        delta_cents=100,
        status="DISCREPANCY",
    )
    row = next(
        r
        for r in taxact_fields.coverage_report(svc.session, ws).rows
        if (r.tax_section, r.field_key) == ("1099-DIV", "dividends.box1a")
    )
    assert row.verdict == taxact_fields.DISCREPANCY
    assert row.delta_cents == 100


# ---------------------------------------------------------------------------
# export: draft vs final, determinism, staleness
# ---------------------------------------------------------------------------


def test_draft_is_allowed_with_gaps_and_labelled_everywhere(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    _seed_investments(svc, ws)  # no comparisons, no scope: plenty of gaps
    dest = tmp_path / "exports" / "draft1"
    result = export_packet(svc.session, ws, is_draft=True, destination=dest)

    assert result.is_draft is True
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    assert manifest["is_draft"] is True
    assert manifest["coverage"]["complete"] is False
    assert "DRAFT" in (dest / CSV_NAME).read_text()
    assert "DRAFT — NOT FOR FILING" in (dest / PACKET_NAME).read_text()


def test_final_refused_with_named_gaps(svc: TaxWorkspaceService, ws: str, tmp_path: Path) -> None:
    _seed_investments(svc, ws)
    with pytest.raises(ExportReadinessError) as err:
        export_packet(svc.session, ws, is_draft=False, destination=tmp_path / "nope")
    message = str(err.value)
    assert "readiness" in message  # scope unconfigured
    assert "coverage" in message and "dividends.box1a" in message
    assert not (tmp_path / "nope").exists()  # nothing half-written


def test_missing_basis_blocks_final_with_a_named_reason(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    doc = _doc(svc, ws, "brokerage_1099.pdf", b"t8b")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 10_000)
    _sale(
        svc, ws, doc.id,
        security_description="NONCOVERED LOT", proceeds_cents=310_000,
        cost_basis_cents=None, gain_loss_cents=None, term="LONG_TERM",
        basis_reported_to_irs=False,
    )
    _configure_scope(svc.session, ws)
    _record_matched_for_required(svc, ws)

    with pytest.raises(ExportReadinessError) as err:
        export_packet(svc.session, ws, is_draft=False, destination=tmp_path / "nope")
    assert "basis" in str(err.value).lower()


def test_clean_workspace_exports_final(svc: TaxWorkspaceService, ws: str, tmp_path: Path) -> None:
    _seed_investments(svc, ws)
    _configure_scope(svc.session, ws)
    _record_matched_for_required(svc, ws)

    coverage = taxact_fields.coverage_report(svc.session, ws)
    assert coverage.complete is True

    dest = tmp_path / "exports" / "final1"
    result = export_packet(svc.session, ws, is_draft=False, destination=dest)
    assert result.is_draft is False
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    assert manifest["is_draft"] is False
    assert manifest["content_hash"] == result.content_hash
    assert "Approved export" in (dest / PACKET_NAME).read_text()
    # The checklist reproduces the reviewed totals.
    csv_text = (dest / CSV_NAME).read_text()
    assert "$3,850.00" in csv_text and "$12,500.00" in csv_text


def test_export_is_deterministic(svc: TaxWorkspaceService, ws: str, tmp_path: Path) -> None:
    _seed_investments(svc, ws)
    first = export_packet(svc.session, ws, is_draft=True, destination=tmp_path / "d1")
    second = export_packet(svc.session, ws, is_draft=True, destination=tmp_path / "d2")
    assert first.content_hash == second.content_hash
    assert (tmp_path / "d1" / CSV_NAME).read_text() == (tmp_path / "d2" / CSV_NAME).read_text()
    assert (tmp_path / "d1" / PACKET_NAME).read_text() == (tmp_path / "d2" / PACKET_NAME).read_text()


def test_changed_evidence_marks_an_export_stale(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    doc = _doc(svc, ws, "brokerage_1099.pdf", b"t8c")
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 385_000)
    _configure_scope(svc.session, ws)
    _record_matched_for_required(svc, ws)
    dest = tmp_path / "exports" / "final"
    export_packet(svc.session, ws, is_draft=False, destination=dest)

    report = verify_export_current(svc.session, ws, dest)
    assert report["stale"] is False

    # Reviewer corrects the dividend figure: the export must no longer pass.
    _field(svc, ws, doc.id, "box1a_total_ordinary_dividends", 400_000)
    _record_matched_for_required(svc, ws)
    report = verify_export_current(svc.session, ws, dest)
    assert report["stale"] is True


def test_resolved_items_retain_explanation_and_evidence(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    _seed_investments(svc, ws)
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1a",
        source_working_paper_cents=385_000,
        taxact_entered_cents=300_000,
        delta_cents=-85_000,
        status="DISCREPANCY",
        explanation="Transposed digits in TaxAct box 1a; corrected on re-entry.",
    )
    # The reviewer re-enters the correct figure: history is retained as STALE.
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="1099-DIV",
        field_key="dividends.box1a",
        source_working_paper_cents=385_000,
        taxact_entered_cents=385_000,
        delta_cents=0,
        status="MATCHED",
        explanation="Re-entered after correcting the transposition.",
    )
    row = next(
        r
        for r in taxact_fields.coverage_report(svc.session, ws).rows
        if (r.tax_section, r.field_key) == ("1099-DIV", "dividends.box1a")
    )
    assert row.verdict == taxact_fields.COVERED
    dest = tmp_path / "exports" / "draft"
    export_packet(svc.session, ws, is_draft=True, destination=dest)
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    # The current row carries the resolving explanation, not the stale one.
    current = next(f for f in manifest["fields"] if f["field_key"] == "dividends.box1a")
    assert "Re-entered" in current["note"]


# ---------------------------------------------------------------------------
# CSV safety and privacy
# ---------------------------------------------------------------------------


def test_formula_like_text_is_csv_safe(svc: TaxWorkspaceService, ws: str, tmp_path: Path) -> None:
    _seed_investments(svc, ws)
    # A reviewer-recorded key is free text: it must not become a formula.
    svc.record_reconciliation_item(
        workspace_id=ws,
        tax_section="Form 8949",
        field_key="=SUM(A1:A9)+CMD|' /C calc'!A0",
        source_working_paper_cents=1,
        taxact_entered_cents=1,
        delta_cents=0,
        status="MATCHED",
        explanation="+42 @hyperlink('http://evil') -2",
    )
    dest = tmp_path / "exports" / "draft"
    export_packet(svc.session, ws, is_draft=True, destination=dest)
    for line in (dest / CSV_NAME).read_text().splitlines():
        if "SUM(A1" in line or "hyperlink" in line:
            assert line.split(",")[0].startswith("'=\"") or "'=" in line or "'," in line

    # Stronger, simpler invariant: no cell begins with a formula character.
    import csv as csv_mod

    with open(dest / CSV_NAME, newline="") as handle:
        for row in csv_mod.reader(handle):
            for cell in row:
                assert not cell.startswith(("=", "+", "@", "\t")), repr(cell)


def test_manifest_carries_no_private_paths_or_raw_documents(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path, tax_env: Path
) -> None:
    _seed_investments(svc, ws)
    dest = tmp_path / "exports" / "draft"
    export_packet(svc.session, ws, is_draft=True, destination=dest)
    manifest_text = (dest / MANIFEST_NAME).read_text()
    assert str(tax_env) not in manifest_text
    assert str(Path.home()) not in manifest_text
    assert "storage_path" not in manifest_text
    manifest = json.loads(manifest_text)
    (doc,) = manifest["documents"]
    assert len(doc["sha256_prefix"]) == 12
    assert set(doc) == {
        "id", "file_name", "doc_type", "sha256_prefix", "is_corrected", "supersedes",
    }


def test_traceability_every_row_cites_its_evidence(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    _seed_investments(svc, ws)
    _configure_scope(svc.session, ws)
    _record_matched_for_required(svc, ws)
    dest = tmp_path / "exports" / "final"
    export_packet(svc.session, ws, is_draft=False, destination=dest)
    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    covered = [f for f in manifest["fields"] if f["verdict"] == taxact_fields.COVERED]
    assert covered
    for row in covered:
        assert row["member_ids"], row["field_key"]
        assert row["citations"], row["field_key"]
    dividends = next(f for f in covered if f["field_key"] == "dividends.box1a")
    assert dividends["citations"][0].startswith("brokerage_1099.pdf")


def test_destination_guards(svc: TaxWorkspaceService, ws: str, tmp_path: Path) -> None:
    from local_ledger.db import REPO_ROOT

    with pytest.raises(TaxExportError, match="inside the repository"):
        export_packet(
            svc.session, ws, is_draft=True, destination=REPO_ROOT / "data" / "nope"
        )
    existing = tmp_path / "already_there"
    existing.mkdir()
    with pytest.raises(TaxExportError, match="already exists"):
        export_packet(svc.session, ws, is_draft=True, destination=existing)


def test_missing_rental_support_blocks_final(
    svc: TaxWorkspaceService, ws: str, tmp_path: Path
) -> None:
    doc = _doc(svc, ws, "rental_statement.pdf", b"t8d")
    prop = _prop(svc, ws, "123 Citrus Lane")
    _exp(svc, ws, prop.id, "Repairs", 142_000, doc_id=doc.id)
    # No depreciation schedule, scope not configured, no comparisons.
    with pytest.raises(ExportReadinessError) as err:
        export_packet(svc.session, ws, is_draft=False, destination=tmp_path / "nope")
    assert "readiness" in str(err.value)


# ---------------------------------------------------------------------------
# page boot
# ---------------------------------------------------------------------------


def test_taxact_comparison_page_boots(apptest) -> None:  # type: ignore[no-untyped-def]
    """The comparison/export page renders against a migrated empty DB."""
    apptest("app_pages/tax_compare.py")
