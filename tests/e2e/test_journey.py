"""End-to-end user journey (subprocess-isolated AppTest boots).

Boots the full app.py (with navigation and chat sidebar) against migrated temp
DBs — the genuine Streamlit surface. Service-level pipeline coverage lives in
tests/unit/test_e2e_journey.py.
"""

from __future__ import annotations


def test_app_boots_and_renders_home(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app.py")


def test_app_boots_with_empty_db(apptest) -> None:  # type: ignore[no-untyped-def]
    apptest("app.py")
