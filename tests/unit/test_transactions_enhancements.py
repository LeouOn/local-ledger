"""AppTest for enhanced transactions page (subprocess-isolated)."""

from __future__ import annotations


def test_transactions_page_renders_with_enhancements(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/transactions.py")
