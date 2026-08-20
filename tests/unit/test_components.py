"""Component wrappers: section_header, kpi_card, status_badge.

These tests stub `streamlit.markdown` directly (AppTest's `from_function`
runs the function in a fresh script context where our module's imports
aren't visible, so we use a simpler monkeypatch pattern instead).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from local_ledger.ui.components import (
    kpi_card,
    section_header,
    status_badge,
)


def _capture_markdown(monkeypatch: Any) -> list[str]:
    """Return a list that captures every body passed to st.markdown."""
    captured: list[str] = []

    def fake_markdown(body: str, **kwargs: Any) -> Any:
        captured.append(body)
        return None

    monkeypatch.setattr(st, "markdown", fake_markdown)
    return captured


def test_section_header_renders_eyebrow_and_title(monkeypatch: Any) -> None:
    captured = _capture_markdown(monkeypatch)
    section_header("OVERVIEW", "Budget vs actual")
    html = "\n".join(captured)
    assert "OVERVIEW" in html
    assert "Budget vs actual" in html
    assert "sb-eyebrow" in html


def test_kpi_card_renders_label_and_value(monkeypatch: Any) -> None:
    captured = _capture_markdown(monkeypatch)
    kpi_card(label="Income", value="$4,200.00")
    html = "\n".join(captured)
    assert "Income" in html
    assert "$4,200.00" in html


def test_kpi_card_renders_delta_with_status_class(monkeypatch: Any) -> None:
    captured = _capture_markdown(monkeypatch)
    kpi_card(label="Spent", value="$3,100.00", delta="+12%", status="bad")
    html = "\n".join(captured)
    assert "Spent" in html
    assert "+12%" in html
    assert "var(--danger)" in html


def test_status_badge_renders_tone_class(monkeypatch: Any) -> None:
    captured = _capture_markdown(monkeypatch)
    status_badge("Over budget", tone="bad")
    html = "\n".join(captured)
    assert "Over budget" in html
    assert "sb-badge" in html
    assert "sb-badge--bad" in html


def test_status_badge_accepts_all_tones(monkeypatch: Any) -> None:
    for tone in ("ok", "warn", "bad", "neutral"):
        captured = _capture_markdown(monkeypatch)
        status_badge("Test", tone=tone)
        html = "\n".join(captured)
        assert f"sb-badge--{tone}" in html
