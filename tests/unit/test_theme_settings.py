"""theme_mode is a persisted field on ProviderSettings (id=1 singleton)."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.models import ProviderSettings


def _new_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return Session(engine)


def test_provider_settings_has_theme_mode_field() -> None:
    s = ProviderSettings()
    assert hasattr(s, "theme_mode")
    assert s.theme_mode == "light"


def test_provider_settings_persists_theme_mode_change() -> None:
    session = _new_session()
    row = ProviderSettings(id=1)
    session.add(row)
    session.commit()
    session.refresh(row)
    # Default is "light"
    assert row.theme_mode == "light"
    # Update to "dark"
    row.theme_mode = "dark"
    session.add(row)
    session.commit()
    session.refresh(row)
    assert row.theme_mode == "dark"


def test_save_settings_round_trips_theme_mode() -> None:
    """The existing providers.save_settings uses **fields; theme_mode must work via that path."""
    from local_ledger.llm.providers import load_settings, save_settings

    session = _new_session()
    row = save_settings(session, theme_mode="dark")
    assert row.theme_mode == "dark"
    again = load_settings(session)
    assert again.theme_mode == "dark"
