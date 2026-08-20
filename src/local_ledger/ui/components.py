"""Thin wrappers over Streamlit primitives.

These three components are the only Python wrappers in the theme system.
Everything else is CSS via inject_css() targeting Streamlit's internal
data-testid selectors. They are wrappers because Streamlit's built-in
elements (st.metric, st.subheader) don't expose the styling hooks we need
to match the design tokens without rewriting their entire DOM.
"""

from __future__ import annotations

from typing import Literal

import streamlit as st


def section_header(eyebrow: str, title: str) -> None:
    """Small uppercase eyebrow above a serif, weight-600 title.

    Use above the first content block on every page.
    """
    st.markdown(
        f'<p class="sb-eyebrow">{eyebrow}</p>\n<h2 style="margin: 0 0 16px 0;">{title}</h2>',
        unsafe_allow_html=True,
    )


def kpi_card(
    label: str,
    value: str,
    delta: str | None = None,
    status: Literal["ok", "warn", "bad"] | None = None,
) -> None:
    """KPI card with consistent typography.

    Replaces raw st.metric calls on Home and Insights. Uses st.metric's DOM
    so the CSS in inject_css() can style it consistently, but the value,
    label, and delta strings are pre-formatted so the CSS receives the
    expected text.
    """
    delta_html = ""
    if delta is not None:
        color_class = ""
        if status == "ok":
            color_class = "color: var(--success);"
        elif status == "warn":
            color_class = "color: var(--warning);"
        elif status == "bad":
            color_class = "color: var(--danger);"
        delta_html = (
            f'<div style="font-size: var(--size-sm); margin-top: 4px; {color_class}">'
            f"{delta}</div>"
        )
    st.markdown(
        f'<div>'
        f'<div style="font-family: var(--font-sans); font-size: var(--size-xs); '
        f'color: var(--ink-muted); text-transform: uppercase; letter-spacing: 0.05em;">'
        f"{label}</div>"
        f'<div style="font-family: var(--font-mono); font-size: var(--size-2xl); '
        f"font-weight: 500; font-variant-numeric: tabular-nums; color: var(--ink);\">"
        f"{value}</div>"
        f"{delta_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


def status_badge(text: str, tone: Literal["ok", "warn", "bad", "neutral"]) -> None:
    """Small inline pill with tone-based tinting."""
    st.markdown(
        f'<span class="sb-badge sb-badge--{tone}">{text}</span>',
        unsafe_allow_html=True,
    )
