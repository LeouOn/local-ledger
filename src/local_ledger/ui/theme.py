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


# -- CSS generation ------------------------------------------------------------

def _tokens_to_css_vars(t: ThemeTokens) -> str:
    """Generate the :root CSS-custom-property block from one token table."""
    pairs = [
        ("--bg", t.bg),
        ("--surface", t.surface),
        ("--ink", t.ink),
        ("--ink-muted", t.ink_muted),
        ("--border", t.border),
        ("--border-subtle", t.border_subtle),
        ("--accent", t.accent),
        ("--accent-hover", t.accent_hover),
        ("--accent-soft", t.accent_soft),
        ("--success", t.success),
        ("--warning", t.warning),
        ("--danger", t.danger),
        ("--neutral-series", t.neutral_series),
        ("--font-sans", t.font_sans),
        ("--font-serif", t.font_serif),
        ("--font-mono", t.font_mono),
        ("--size-xs", t.size_xs),
        ("--size-sm", t.size_sm),
        ("--size-base", t.size_base),
        ("--size-lg", t.size_lg),
        ("--size-xl", t.size_xl),
        ("--size-2xl", t.size_2xl),
        ("--size-3xl", t.size_3xl),
        ("--radius-sm", t.radius_sm),
        ("--radius-md", t.radius_md),
        ("--radius-lg", t.radius_lg),
        ("--shadow-card", t.shadow_card),
    ]
    lines = [f"  {name}: {value};" for name, value in pairs]
    return ":root {\n" + "\n".join(lines) + "\n}"


def _surface_css() -> str:
    """Common surface rules. All selectors are Streamlit-internal data-testid values."""
    return """
/* Root */
.stApp, header[data-testid="stHeader"] {
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font-sans);
  font-size: var(--size-base);
}

/* Sidebar */
[data-testid="stSidebar"] {
  background: var(--bg);
  border-right: 1px solid var(--border);
}

/* Cards / panels (nested vertical blocks) */
[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-card);
  padding: var(--size-base);
}

/* Tables */
[data-testid="stDataFrame"], [data-testid="stDataEditor"] {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
[data-testid="stDataFrame"] th, [data-testid="stDataEditor"] th {
  font-weight: 500;
  color: var(--ink-muted);
  background: var(--bg);
}

/* Forms */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stDateInput"] input,
[data-baseweb="select"] {
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--ink);
}

/* Buttons */
button[kind="primary"] {
  background: var(--accent);
  color: var(--surface);
  border: none;
  border-radius: var(--radius-sm);
  font-weight: 500;
}
button[kind="primary"]:hover {
  background: var(--accent-hover);
}
button[kind="secondary"] {
  background: var(--surface);
  color: var(--ink);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

/* Metrics */
[data-testid="stMetric"] label {
  color: var(--ink-muted);
  font-size: var(--size-xs);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: var(--font-sans);
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
  font-family: var(--font-mono);
  font-size: var(--size-2xl);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

/* Alerts */
[data-testid="stAlert"] {
  border-radius: var(--radius-sm);
}

/* Tabs */
[data-baseweb="tab"] {
  color: var(--ink-muted);
}
[data-baseweb="tab"][aria-selected="true"] {
  color: var(--ink);
  border-bottom: 2px solid var(--accent);
}

/* Code */
[data-testid="stCode"] {
  font-family: var(--font-mono);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

/* Headlines (page titles + section headers) */
h1, h2, h3 {
  font-family: var(--font-serif);
  font-weight: 600;
  color: var(--ink);
}
h1 { font-size: var(--size-3xl); }
h2 { font-size: var(--size-xl); }
h3 { font-size: var(--size-lg); }

/* Status badges (used by status_badge wrapper) */
.sb-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--size-xs);
  font-family: var(--font-sans);
  font-weight: 500;
  line-height: 1.4;
}
.sb-badge--ok { background: var(--accent-soft); color: var(--accent); }
.sb-badge--warn { background: rgba(217, 119, 6, 0.15); color: var(--warning); }
.sb-badge--bad { background: rgba(220, 38, 38, 0.15); color: var(--danger); }
.sb-badge--neutral { background: var(--border-subtle); color: var(--ink-muted); }

/* Eyebrow (used by section_header wrapper) */
.sb-eyebrow {
  font-family: var(--font-serif);
  font-size: var(--size-xs);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--ink-muted);
  margin: 0 0 4px 0;
}

/* Focus rings */
*:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
"""


def _font_face_css() -> str:
    """@font-face rule with base64-inlined Alegreya variable font.

    Streamlit does not serve arbitrary files from the repo, so a plain
    `url("/path/to/font.woff2")` cannot be resolved by the browser. Inlining
    the font as a data URL keeps the CSS bundle self-contained and offline.

    Google Fonts v41 serves Alegreya as a single variable font covering the
    `wght` axis 400-700. The vendored `alegreya-regular.woff2` and
    `alegreya-bold.woff2` files are byte-identical (Task 1 discovered this);
    we inline the file once and declare a single `@font-face` with the
    `font-weight: 400 700` range so the browser picks the right axis value
    from the same data URL. Inlining both files separately would duplicate
    ~43 KB of base64 for no benefit.
    """
    import base64

    fonts_dir = font_files_dir()
    font_b64 = base64.b64encode((fonts_dir / "alegreya-regular.woff2").read_bytes()).decode()
    return f"""
@font-face {{
  font-family: "Alegreya";
  font-weight: 400 700;
  font-style: normal;
  src: url(data:font/woff2;base64,{font_b64}) format("woff2");
}}
"""


def build_css(mode: Literal["light", "dark"]) -> str:
    """Return a single CSS string derived from the active tokens."""
    t = TOKENS_LIGHT if mode == "light" else TOKENS_DARK
    return "\n".join([
        _font_face_css(),
        _tokens_to_css_vars(t),
        _surface_css(),
    ])


def inject_css(mode: Literal["light", "dark"]) -> None:
    """Emit one <style> block derived from active tokens via st.markdown."""
    import streamlit as st

    set_active_mode(mode)
    css = build_css(mode)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# -- Stubs to be implemented in Task 4 ---------------------------------------

def apply_chart_colors(chart_call_fn, *args, **kwargs) -> None:
    """Wrap a chart call so it picks up palette_for_chart() via color=. Implemented in Task 4."""
    raise NotImplementedError("implemented in Task 4")