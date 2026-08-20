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