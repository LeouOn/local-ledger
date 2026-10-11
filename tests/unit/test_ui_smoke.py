"""UI smoke tests: each page boots in a fresh subprocess (hermetic).

Page-load tests run via the ``apptest`` fixture (subprocess per page) because
Streamlit leaks form/widget state across ``AppTest.run()`` calls in one process,
which made these tests order-dependent in full-suite runs.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LOCAL_LEDGER_SKIP_UI_SMOKE") == "1",
    reason="UI smoke skipped by env",
)


def test_app_entry_boots(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app.py")


def test_home_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/home.py")


def test_transactions_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/transactions.py")


def test_budgets_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/budgets.py")


def test_import_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/import_.py")


def test_insights_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/insights.py")


def test_settings_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/settings.py")


def test_tax_intake_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/tax_intake.py")


def test_tax_intake_inspector_renders_saved_document(apptest, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sqlmodel import Session, create_engine

    from local_ledger.tax.models import DocumentType
    from local_ledger.tax.service import TaxWorkspaceService
    from tests.unit.tax.pdf_fixtures import text_pdf

    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tmp_path / "documents"))
    engine = create_engine(apptest.db_url)
    with Session(engine) as session:
        svc = TaxWorkspaceService(session)
        ws = svc.get_or_create_workspace(2025)
        svc.register_document(
            ws.id, "synthetic-1099-int.pdf", text_pdf(["Interest income Box 1: $25.00"]),
            doc_type=DocumentType.FORM_1099_INT,
        )
    engine.dispose()
    apptest("app_pages/tax_intake.py")


@pytest.mark.parametrize("page", ["tax_investments", "tax_rentals", "tax_compare"])
def test_tax_audit_page_renders(apptest, page) -> None:  # type: ignore[no-untyped-def]
    apptest(f"app_pages/{page}.py")


def test_tax_california_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/tax_california.py")
