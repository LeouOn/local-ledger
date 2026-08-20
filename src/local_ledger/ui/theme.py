"""Design tokens + CSS injection for Local Ledger.

The single source of truth for visual decisions. All page-level styling
(colors, typography, spacing, shadows) is derived from these constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# -- Vendored font directory (relative to this file) ----------------------------

def font_files_dir() -> Path:
    """Return the directory containing the vendored Alegreya .woff2 files."""
    return Path(__file__).resolve().parent / "fonts"


# -- Token dataclass -----------------------------------------------------------

@dataclass(frozen=True)
class ThemeTokens:
    # Colors (light or dark)
    bg: str
    surface: str
    ink: str
    ink_muted: str
    border: str
    border_subtle: str
    accent: str
    accent_hover: str
    accent_soft: str
    success: str
    warning: str
    danger: str
    neutral_series: str
    chart_ramp: tuple[str, ...]

    # Fonts
    font_sans: str
    font_serif: str
    font_mono: str

    # Type sizes (rem)
    size_xs: str
    size_sm: str
    size_base: str
    size_lg: str
    size_xl: str
    size_2xl: str
    size_3xl: str

    # Radii (px)
    radius_sm: str
    radius_md: str
    radius_lg: str

    # Shadows
    shadow_card: str


# -- Font stacks ------------------------------------------------------------------

_SANS = ('system-ui, -apple-system, "Segoe UI", Roboto, Inter, sans-serif')
_SERIF = ('"Alegreya", "Alegreya Sans", Georgia, "Times New Roman", serif')
_MONO = ('ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace')


# -- Two preset table ------------------------------------------------------------

TOKENS_LIGHT = ThemeTokens(
    bg="#FAFAF7",
    surface="#FFFFFF",
    ink="#0F0F10",
    ink_muted="#5C5C5C",
    border="#E5E5E1",
    border_subtle="#EFEFEC",
    accent="#0F766E",
    accent_hover="#115E59",
    accent_soft="#CCFBF1",
    success="#059669",
    warning="#D97706",
    danger="#DC2626",
    neutral_series="#A1A1A1",
    chart_ramp=("#CCFBF1", "#99F6E4", "#5EEAD4", "#2DD4BF", "#14B8A6"),
    font_sans=_SANS,
    font_serif=_SERIF,
    font_mono=_MONO,
    size_xs="0.75rem",
    size_sm="0.875rem",
    size_base="1rem",
    size_lg="1.125rem",
    size_xl="1.25rem",
    size_2xl="1.5rem",
    size_3xl="2rem",
    radius_sm="6px",
    radius_md="10px",
    radius_lg="16px",
    shadow_card=("0 1px 2px rgba(0,0,0,.04), 0 2px 8px rgba(0,0,0,.04)"),
)

TOKENS_DARK = ThemeTokens(
    bg="#0E0E10",
    surface="#1A1A1F",
    ink="#F5F5F2",
    ink_muted="#A1A1A1",
    border="#2A2A2E",
    border_subtle="#1F1F22",
    accent="#14B8A6",
    accent_hover="#2DD4BF",
    accent_soft="#1A2E2C",
    success="#10B981",
    warning="#F59E0B",
    danger="#F87171",
    neutral_series="#5C5C5C",
    chart_ramp=("#5EEAD4", "#2DD4BF", "#14B8A6", "#0D9488", "#0F766E"),
    font_sans=_SANS,
    font_serif=_SERIF,
    font_mono=_MONO,
    size_xs="0.75rem",
    size_sm="0.875rem",
    size_base="1rem",
    size_lg="1.125rem",
    size_xl="1.25rem",
    size_2xl="1.5rem",
    size_3xl="2rem",
    radius_sm="6px",
    radius_md="10px",
    radius_lg="16px",
    shadow_card=("0 1px 2px rgba(0,0,0,.4), 0 2px 8px rgba(0,0,0,.3)"),
)


# -- Active mode (session-scoped; set by app.py at startup) -------------------

_active_mode: Literal["light", "dark"] = "light"


def set_active_mode(mode: Literal["light", "dark"]) -> None:
    """Set the in-process active mode. Called by app.py at startup + on toggle."""
    global _active_mode
    if mode not in ("light", "dark"):
        raise ValueError(f"mode must be 'light' or 'dark', got {mode!r}")
    _active_mode = mode


def get_active_mode() -> Literal["light", "dark"]:
    return _active_mode


def active_tokens() -> ThemeTokens:
    return TOKENS_LIGHT if _active_mode == "light" else TOKENS_DARK


# -- Chart palette (delegate to active tokens) -------------------------------

def palette_for_chart() -> list[str]:
    """Return the 5-step emerald ramp for the current mode."""
    return list(active_tokens().chart_ramp)


# -- Stubs to be implemented in Task 3 / 4 -----------------------------------

def inject_css(mode: Literal["light", "dark"]) -> None:
    """Emit a single <style> block derived from active tokens. Implemented in Task 3."""
    raise NotImplementedError("implemented in Task 3")


def apply_chart_colors(chart_call_fn, *args, **kwargs) -> None:
    """Wrap a chart call so it picks up palette_for_chart() via color=. Implemented in Task 4."""
    raise NotImplementedError("implemented in Task 4")