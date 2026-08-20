"""Theme tokens: light/dark invariants, chart ramp ordering, font-file paths."""

from __future__ import annotations

from local_ledger.ui.theme import (
    TOKENS_DARK,
    TOKENS_LIGHT,
    ThemeTokens,
)

REQUIRED_COLOR_KEYS = {
    "bg", "surface", "ink", "ink_muted", "border", "border_subtle",
    "accent", "accent_hover", "accent_soft",
    "success", "warning", "danger", "neutral_series",
}
REQUIRED_SIZE_KEYS = {"size_xs", "size_sm", "size_base", "size_lg", "size_xl", "size_2xl", "size_3xl"}
REQUIRED_RADIUS_KEYS = {"radius_sm", "radius_md", "radius_lg"}


def _color_keys(tokens: ThemeTokens) -> set[str]:
    return {k for k in vars(tokens) if k in REQUIRED_COLOR_KEYS}


def test_light_tokens_have_required_color_keys() -> None:
    assert REQUIRED_COLOR_KEYS.issubset(_color_keys(TOKENS_LIGHT))


def test_dark_tokens_have_required_color_keys() -> None:
    assert REQUIRED_COLOR_KEYS.issubset(_color_keys(TOKENS_DARK))


def test_light_tokens_have_required_size_keys() -> None:
    keys = {k for k in vars(TOKENS_LIGHT) if k in REQUIRED_SIZE_KEYS}
    assert REQUIRED_SIZE_KEYS.issubset(keys)


def test_dark_tokens_have_required_size_keys() -> None:
    keys = {k for k in vars(TOKENS_DARK) if k in REQUIRED_SIZE_KEYS}
    assert REQUIRED_SIZE_KEYS.issubset(keys)


def test_chart_ramps_have_five_colors() -> None:
    assert len(TOKENS_LIGHT.chart_ramp) == 5
    assert len(TOKENS_DARK.chart_ramp) == 5


def test_light_chart_ramp_is_monotonic_in_luminance() -> None:
    """Light ramp should go from light to dark (left-to-right)."""
    ramp = TOKENS_LIGHT.chart_ramp
    # CCFBF1 (~0.97) > 99F6E4 (~0.93) > 5EEAD4 (~0.82) > 2DD4BF (~0.71) > 14B8A6 (~0.55)
    expected = ["#CCFBF1", "#99F6E4", "#5EEAD4", "#2DD4BF", "#14B8A6"]
    assert ramp == tuple(expected)


def test_dark_chart_ramp_is_monotonic_in_luminance() -> None:
    """Dark ramp should go from light to dark (left-to-right)."""
    ramp = TOKENS_DARK.chart_ramp
    expected = ["#5EEAD4", "#2DD4BF", "#14B8A6", "#0D9488", "#0F766E"]
    assert ramp == tuple(expected)


def test_light_accent_is_teal_700() -> None:
    assert TOKENS_LIGHT.accent == "#0F766E"


def test_dark_accent_is_teal_500() -> None:
    assert TOKENS_DARK.accent == "#14B8A6"


def test_palette_for_chart_returns_five_strings() -> None:
    # Stash the "current mode" via module-level setter (added in Step 3).
    pass  # implemented in Task 3 — placeholder


# -- CSS injection (Task 3) -----------------------------------------------------

from local_ledger.ui.theme import build_css, inject_css  # noqa: E402


def test_build_css_returns_parseable_string() -> None:
    css = build_css("light")
    assert isinstance(css, str)
    # No orphan braces
    assert css.count("{") == css.count("}"), f"unbalanced braces:\n{css}"
    # Contains at least one selector targeting a Streamlit data-testid
    assert "[data-testid=" in css


def test_build_css_contains_font_face_for_alegreya() -> None:
    css = build_css("light")
    assert "@font-face" in css
    assert "Alegreya" in css
    # URL must reference the vendored file, not a CDN
    assert "fonts.gstatic.com" not in css
    assert "fonts.googleapis.com" not in css


def test_build_css_uses_active_token_hex_values() -> None:
    css_light = build_css("light")
    css_dark = build_css("dark")
    # Each must embed its accent hex
    assert "#0F766E" in css_light  # light accent
    assert "#14B8A6" in css_dark   # dark accent
    # And must NOT cross-contaminate
    assert "#0F766E" not in css_dark
    assert "#14B8A6" not in css_light


def test_build_css_includes_required_selectors() -> None:
    """Theme must target every surface listed in the spec's CSS coverage table."""
    css = build_css("light")
    required = [
        "stDataFrame",
        "stDataEditor",
        "stTextInput",
        "stMetric",
        "stAlert",
        "stSidebar",
        "stCode",
        "stVerticalBlock",
    ]
    for sel in required:
        assert sel in css, f"missing selector {sel!r} in CSS"


def test_inject_css_calls_st_markdown(monkeypatch) -> None:
    """inject_css must emit CSS via st.markdown(unsafe_allow_html=True)."""
    captured: list[str] = []

    import streamlit as st

    def fake_markdown(body, **kwargs):
        captured.append(body)
        if "unsafe_allow_html" in kwargs:
            assert kwargs["unsafe_allow_html"] is True

    monkeypatch.setattr(st, "markdown", fake_markdown)
    inject_css("dark")
    assert len(captured) == 1
    assert "[data-testid=" in captured[0]