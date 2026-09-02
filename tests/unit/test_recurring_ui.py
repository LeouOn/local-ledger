"""Recurring detection UI boot test (subprocess-isolated)."""

from __future__ import annotations


def test_recurring_section_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/transactions.py")
