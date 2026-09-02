"""Insights page boot tests (subprocess-isolated)."""

from __future__ import annotations


def test_insights_page_renders_new_sections(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/insights.py")


def test_insights_page_renders_with_empty_db(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app_pages/insights.py")
