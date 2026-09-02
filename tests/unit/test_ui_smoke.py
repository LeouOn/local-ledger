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
