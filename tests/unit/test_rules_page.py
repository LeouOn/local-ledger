"""Rules page boot test (subprocess-isolated)."""

from __future__ import annotations


def test_rules_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/rules.py")
