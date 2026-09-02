"""Goals page boot test (subprocess-isolated)."""

from __future__ import annotations


def test_goals_page_renders(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/goals.py")
