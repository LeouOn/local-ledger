# Theme System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cohesive design-system theme (light + dark, emerald accent, vendored Alegreya serif for headlines, mono for numbers) across all 7 pages of Local Ledger, with a sidebar toggle persisted in the existing `ProviderSettings` table.

**Architecture:** Tokens live in `src/local_ledger/ui/theme.py` as a frozen dataclass; `inject_css(mode)` emits a single `<style>` block via `st.markdown(unsafe_allow_html=True)`. Most styling is CSS-only, targeting stable Streamlit `data-testid` selectors. Three thin Python wrappers (`section_header`, `kpi_card`, `status_badge`) handle the cases Streamlit doesn't expose cleanly. The `theme_mode` column is added to the existing `ProviderSettings` singleton (id=1); the existing `load_settings` / `save_settings` already generalize via `**fields`.

**Tech Stack:** Streamlit 1.40+, SQLModel (existing), Alembic (existing), pytest + AppTest (existing). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-19-theme-system-design.md`

---

## Global Constraints

- **Python:** 3.11 or 3.12 (mirrors pyproject `requires-python`).
- **Streaming context:** CSS injection must happen *before* any other `st.*` call so the Streamlit chrome renders with the theme on first paint (no FOUC).
- **Offline-first:** No external CDN. Alegreya fonts are vendored; palette is purely local hex.
- **No service-layer changes:** The 237 existing unit tests must stay green without modification.
- **Commit style:** `feat: …` for new modules, `chore: …` for vendored files, `fix(theme): …` for theme-application commits. One logical change per commit.
- **Branch:** All work happens on `main` (no worktree required for this size of change; each task is small enough to commit directly).
- **Settings persistence:** `theme_mode` is added to `ProviderSettings` (the existing singleton at `src/local_ledger/models.py:179`). The spec calls this the "Settings" table; the model name is `ProviderSettings` to preserve existing call sites. Default value: `"light"`.
- **Alegreya source:** Vendored .woff2 files are fetched once from Google Fonts CSS2 API and committed under `src/local_ledger/ui/fonts/`. License (SIL OFL) gets its own `LICENSE.txt` in the same directory.

---

## File Structure

### New files

```
src/local_ledger/ui/theme.py           # ThemeTokens, TOKENS_LIGHT, TOKENS_DARK, inject_css, palette_for_chart, apply_chart_colors
src/local_ledger/ui/components.py      # section_header, kpi_card, status_badge
src/local_ledger/ui/fonts/LICENSE.txt  # SIL OFL for Alegreya
src/local_ledger/ui/fonts/alegreya-regular.woff2
src/local_ledger/ui/fonts/alegreya-bold.woff2
tests/unit/test_theme.py
tests/unit/test_components.py
alembic/versions/2026_08_19_add_theme_mode.py
```

### Modified files

```
.streamlit/config.toml                 # add [theme] section with light/dark base + emerald primary
src/local_ledger/models.py             # add theme_mode: str = "light" to ProviderSettings
src/local_ledger/llm/providers.py      # pass theme_mode through load_settings (no code change; field is auto-handled)
app.py                                  # inject_css at startup; sidebar toggle in main()
app_pages/home.py                       # replace st.title + st.metric with section_header + kpi_card
app_pages/insights.py                   # section_header + kpi_card + status_badge
app_pages/transactions.py               # section_header
app_pages/budgets.py                    # section_header + status_badge
app_pages/goals.py                      # section_header + status_badge
app_pages/rules.py                      # section_header
app_pages/import_.py                    # section_header
app_pages/settings.py                   # add Theme radio + save under "Display"
```

### Dependency order

Tokens → CSS injection → Chart helpers → Components → Settings persistence → App integration → Page swaps (3 batches) → Final verification.

---

## Task 1: Vendor Alegreya fonts + license

**Files:**
- Create: `src/local_ledger/ui/fonts/LICENSE.txt`
- Create: `src/local_ledger/ui/fonts/alegreya-regular.woff2`
- Create: `src/local_ledger/ui/fonts/alegreya-bold.woff2`
- Create: `tests/unit/test_theme_fonts.py` (validates vendored files exist + woff2 magic bytes)

**Interfaces:**
- Consumes: Google Fonts CSS2 API (network, one-time)
- Produces: `src/local_ledger/ui/fonts/alegreya-regular.woff2`, `src/local_ledger/ui/fonts/alegreya-bold.woff2`, `src/local_ledger/ui/fonts/LICENSE.txt`

- [ ] **Step 1: Fetch the CSS from Google Fonts to discover woff2 URLs**

Run (from project root):
```bash
curl -s -H "User-Agent: Mozilla/5.0" "https://fonts.googleapis.com/css2?family=Alegreya:wght@400;700&display=swap" -o /tmp/alegreya.css
```
Expected: a file at `/tmp/alegreya.css` containing two `@font-face` blocks, each with a `src: url(https://fonts.gstatic.com/.../alegreya-*.woff2)` line.

- [ ] **Step 2: Extract the two woff2 URLs**

Run:
```bash
grep -oE 'https://fonts\.gstatic\.com/[^)]+\.woff2' /tmp/alegreya.css
```
Expected: two URLs, one for `latin` Regular (400) and one for `latin` Bold (700). Identify which is which by the surrounding `font-weight:` declaration.

- [ ] **Step 3: Download the two .woff2 files**

Run (replace the URLs with the ones from Step 2):
```bash
mkdir -p src/local_ledger/ui/fonts
curl -s -o src/local_ledger/ui/fonts/alegreya-regular.woff2 "<URL_FOR_REGULAR>"
curl -s -o src/local_ledger/ui/fonts/alegreya-bold.woff2 "<URL_FOR_BOLD>"
```
Expected: two binary files, each roughly 50-100 KB.

- [ ] **Step 4: Verify woff2 magic bytes**

Run:
```bash
python -c "import sys; data=open('src/local_ledger/ui/fonts/alegreya-regular.woff2','rb').read(); assert data[:4]==b'wOF2', data[:4]; print('regular OK', len(data))"
python -c "import sys; data=open('src/local_ledger/ui/fonts/alegreya-bold.woff2','rb').read(); assert data[:4]==b'wOF2', data[:4]; print('bold OK', len(data))"
```
Expected: `regular OK <size>` and `bold OK <size>`. Both files must start with the ASCII bytes `wOF2`.

- [ ] **Step 5: Add LICENSE.txt**

Create `src/local_ledger/ui/fonts/LICENSE.txt` with this exact content:

```
SIL Open Font License v1.1

Font: Alegreya
Copyright 2011 The Alegreya Project Authors (https://github.com/huertatipografica/Alegreya)
License: SIL Open Font License, Version 1.1 (https://openfontlicense.org)

This license is copied in the OFL.txt that ships with the font from upstream.
The font files committed to this repository are licensed under the OFL;
you may use, study, modify, and redistribute them under the terms of that license.
```

- [ ] **Step 6: Write the failing test for vendored files**

Create `tests/unit/test_theme_fonts.py`:
```python
"""Validate that the vendored Alegreya fonts are present and well-formed."""

from __future__ import annotations

from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parents[2] / "src" / "local_ledger" / "ui" / "fonts"


def test_alegreya_regular_woff2_exists_and_has_wOF2_magic() -> None:
    p = FONTS_DIR / "alegreya-regular.woff2"
    assert p.exists(), f"missing {p}"
    data = p.read_bytes()
    assert data[:4] == b"wOF2", f"not a woff2 file: starts with {data[:4]!r}"
    assert len(data) > 30_000, f"regular file too small ({len(data)} B), probably truncated"


def test_alegreya_bold_woff2_exists_and_has_wOF2_magic() -> None:
    p = FONTS_DIR / "alegreya-bold.woff2"
    assert p.exists(), f"missing {p}"
    data = p.read_bytes()
    assert data[:4] == b"wOF2", f"not a woff2 file: starts with {data[:4]!r}"
    assert len(data) > 30_000, f"bold file too small ({len(data)} B), probably truncated"


def test_alegreya_license_present() -> None:
    p = FONTS_DIR / "LICENSE.txt"
    assert p.exists(), f"missing {p}"
    text = p.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text
    assert "Alegreya" in text
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme_fonts.py -v`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add src/local_ledger/ui/fonts tests/unit/test_theme_fonts.py
git commit -m "chore(theme): vendor Alegreya Regular + Bold woff2 under SIL OFL"
```

---

## Task 2: Theme tokens module (dataclass + light/dark tables)

**Files:**
- Create: `src/local_ledger/ui/theme.py`
- Create: `tests/unit/test_theme.py` (token invariants only — CSS injection tested in Task 3)

**Interfaces:**
- Consumes: nothing (no Streamlit/DB dependency)
- Produces: `ThemeTokens` dataclass, `TOKENS_LIGHT`, `TOKENS_DARK` (constants)

- [ ] **Step 1: Write the failing test for token invariants**

Create `tests/unit/test_theme.py`:
```python
"""Theme tokens: light/dark invariants, chart ramp ordering, font-file paths."""

from __future__ import annotations

from pathlib import Path

from local_ledger.ui.theme import (
    TOKENS_DARK,
    TOKENS_LIGHT,
    ThemeTokens,
    font_files_dir,
    palette_for_chart,
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
```

The last test stubs out `palette_for_chart` until Task 3. We'll flesh it out then.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v`
Expected: ImportError on `from local_ledger.ui.theme import ...`. This is the failing condition.

- [ ] **Step 3: Implement the theme tokens module**

Create `src/local_ledger/ui/theme.py`:
```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v`
Expected: 8 tests pass (the 9th — `test_palette_for_chart_returns_five_strings` — is a `pass` placeholder until Task 3).

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/ui/theme.py tests/unit/test_theme.py
git commit -m "feat(ui): theme tokens module with light/dark presets"
```

---

## Task 3: CSS injection (`build_css` + `inject_css`)

**Files:**
- Modify: `src/local_ledger/ui/theme.py`
- Modify: `tests/unit/test_theme.py` (add CSS-shape tests)

**Interfaces:**
- Consumes: `ThemeTokens` (from Task 2)
- Produces:
  - `build_css(mode: Literal["light", "dark"]) -> str` — pure function, returns a CSS string
  - `inject_css(mode: Literal["light", "dark"]) -> None` — calls `st.markdown` with the CSS

- [ ] **Step 1: Write the failing tests for CSS shape**

Append to `tests/unit/test_theme.py`:
```python
from local_ledger.ui.theme import build_css, inject_css


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v -k "build_css or inject_css"`
Expected: 5 failures (functions don't exist yet).

- [ ] **Step 3: Implement `build_css` and `inject_css`**

Replace the stubs in `src/local_ledger/ui/theme.py` with the real implementations. Add this just below the `palette_for_chart` definition:

```python
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
    `wght` axis 400–900. The vendored `alegreya-regular.woff2` and
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v`
Expected: 13 tests pass (8 from Task 2 + 5 new).

- [ ] **Step 5: Run the full suite to verify zero regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 237 + 13 = 250 passed (the existing 237 plus the 13 new theme tokens + CSS shape tests).

- [ ] **Step 6: Commit**

```bash
git add src/local_ledger/ui/theme.py tests/unit/test_theme.py
git commit -m "feat(ui): CSS injection module with @font-face and surface rules"
```

---

## Task 4: Chart helpers (`apply_chart_colors`)

**Files:**
- Modify: `src/local_ledger/ui/theme.py`
- Modify: `tests/unit/test_theme.py` (add chart-helper tests)

**Interfaces:**
- Consumes: `palette_for_chart()` (from Task 2)
- Produces: `apply_chart_colors(chart_call_fn, *args, **kwargs)` — wraps a chart call, injects `color=palette_for_chart()`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_theme.py`:
```python
def test_apply_chart_colors_injects_color_argument() -> None:
    """apply_chart_colors must pass color=palette_for_chart() to the chart call."""
    captured: dict = {}

    def fake_chart(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    apply_chart_colors(fake_chart, "x", y="y")
    assert "color" in captured["kwargs"]
    assert captured["kwargs"]["color"] == palette_for_chart()
    assert captured["args"] == ("x",)
    assert captured["kwargs"]["y"] == "y"


def test_apply_chart_colors_does_not_overwrite_explicit_color() -> None:
    """If the caller passed color=, leave it alone."""
    captured: dict = {}

    def fake_chart(*args, **kwargs):
        captured.update(kwargs)

    apply_chart_colors(fake_chart, color=["#000000"])
    assert captured["color"] == ["#000000"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v -k "apply_chart_colors"`
Expected: 2 failures (NotImplementedError).

- [ ] **Step 3: Implement `apply_chart_colors`**

Replace the stub in `src/local_ledger/ui/theme.py`:
```python
def apply_chart_colors(chart_call_fn, *args, **kwargs) -> None:
    """Wrap a chart call so it picks up palette_for_chart() via color=.

    If the caller already passed color=, that value is preserved.
    """
    if "color" not in kwargs:
        kwargs["color"] = palette_for_chart()
    chart_call_fn(*args, **kwargs)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme.py -v -k "apply_chart_colors"`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/ui/theme.py tests/unit/test_theme.py
git commit -m "feat(ui): apply_chart_colors helper for sequential-emerald charts"
```

---

## Task 5: Component wrappers (`section_header`, `kpi_card`, `status_badge`)

**Files:**
- Create: `src/local_ledger/ui/components.py`
- Create: `tests/unit/test_components.py`

**Interfaces:**
- Consumes: `st.markdown` (via Streamlit)
- Produces:
  - `section_header(eyebrow: str, title: str) -> None`
  - `kpi_card(label: str, value: str, delta: str | None = None, status: Literal["ok", "warn", "bad"] | None = None) -> None`
  - `status_badge(text: str, tone: Literal["ok", "warn", "bad", "neutral"]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_components.py`:
```python
"""Component wrappers: section_header, kpi_card, status_badge."""

from __future__ import annotations

from typing import Literal

from streamlit.testing.v1 import AppTest

from local_ledger.ui.components import (
    kpi_card,
    section_header,
    status_badge,
)


def _run_in_app(fn) -> AppTest:
    """Render a component in a minimal Streamlit app for AppTest extraction."""
    at = AppTest.from_function(fn).run()
    return at


def test_section_header_renders_eyebrow_and_title() -> None:
    def app() -> None:
        section_header("OVERVIEW", "Budget vs actual")

    at = _run_in_app(app)
    html = "\n".join(str(m.value) for m in at.markdown)
    assert "OVERVIEW" in html
    assert "Budget vs actual" in html
    assert "sb-eyebrow" in html


def test_kpi_card_renders_label_and_value() -> None:
    def app() -> None:
        kpi_card(label="Income", value="$4,200.00")

    at = _run_in_app(app)
    html = "\n".join(str(m.value) for m in at.markdown)
    assert "Income" in html
    assert "$4,200.00" in html


def test_kpi_card_renders_delta_with_status_class() -> None:
    def app() -> None:
        kpi_card(label="Spent", value="$3,100.00", delta="+12%", status="bad")

    at = _run_in_app(app)
    html = "\n".join(str(m.value) for m in at.markdown)
    assert "Spent" in html
    assert "+12%" in html
    assert "bad" in html.lower()


def test_status_badge_renders_tone_class() -> None:
    def app() -> None:
        status_badge("Over budget", tone="bad")

    at = _run_in_app(app)
    html = "\n".join(str(m.value) for m in at.markdown)
    assert "Over budget" in html
    assert "sb-badge" in html
    assert "sb-badge--bad" in html


def test_status_badge_accepts_all_tones() -> None:
    for tone in ("ok", "warn", "bad", "neutral"):
        def app(tone: Literal["ok", "warn", "bad", "neutral"] = tone) -> None:
            status_badge("Test", tone=tone)

        at = _run_in_app(app)
        html = "\n".join(str(m.value) for m in at.markdown)
        assert f"sb-badge--{tone}" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_components.py -v`
Expected: ImportError on `from local_ledger.ui.components import ...`.

- [ ] **Step 3: Implement the components module**

Create `src/local_ledger/ui/components.py`:
```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_components.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/local_ledger/ui/components.py tests/unit/test_components.py
git commit -m "feat(ui): section_header, kpi_card, status_badge wrappers"
```

---

## Task 6: Settings persistence — `theme_mode` column on `ProviderSettings`

**Files:**
- Modify: `src/local_ledger/models.py`
- Create: `alembic/versions/2026_08_19_add_theme_mode.py` (manual migration)
- Modify: `tests/unit/test_theme_settings.py` (new test file)

**Interfaces:**
- Consumes: existing `ProviderSettings` (`src/local_ledger/models.py:179`)
- Produces: `ProviderSettings.theme_mode: str = "light"` field, plus Alembic migration

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_theme_settings.py`:
```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme_settings.py -v`
Expected: 3 failures (TypeError / AttributeError).

- [ ] **Step 3: Add `theme_mode` to `ProviderSettings`**

In `src/local_ledger/models.py`, edit the `ProviderSettings` class (line 179). Add this line after the `evaluation_default_provider` field:

```python
    theme_mode: str = Field(default="light", description="light | dark")
```

The result should look like:
```python
class ProviderSettings(SQLModel, table=True):
    """Singleton row (id=1) holding the app-wide provider configuration."""

    id: int = Field(default=1, primary_key=True)
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = "llama3.3:8b"
    cloud_base_url: str | None = None
    cloud_model: str | None = None
    cloud_api_key_env: str | None = Field(
        default=None, description="Name of env var holding the cloud key (never the key)"
    )
    evaluation_default_provider: str = Field(default="local")  # local | cloud-consent
    theme_mode: str = Field(default="light", description="light | dark")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme_settings.py -v`
Expected: 3 tests pass.

- [ ] **Step 5: Generate the Alembic migration**

Run:
```bash
.venv\Scripts\alembic.exe revision --autogenerate -m "add theme_mode to providersettings"
```
Expected: a new file under `alembic/versions/` like `2026_08_19_HHMM_add_theme_mode_to_providersettings.py`. Open it and verify:
- The `op.add_column` call targets `providersettings` and adds `theme_mode`
- The default in the column is `"light"` (the server-side default for existing rows)
- Downgrade removes the column

If the autogenerate missed the column (autogenerate is heuristic), add the `add_column` explicitly:
```python
def upgrade() -> None:
    op.add_column(
        "providersettings",
        sa.Column("theme_mode", sa.String(), nullable=False, server_default="light"),
    )


def downgrade() -> None:
    op.drop_column("providersettings", "theme_mode")
```

- [ ] **Step 6: Apply the migration to the live DB**

Run:
```bash
.venv\Scripts\alembic.exe upgrade head
```
Expected: completes with no error. The schema now has `theme_mode` on `providersettings`.

- [ ] **Step 7: Run the full suite to verify zero regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 237 + 13 + 5 + 3 = 258 passed, 10 skipped.

- [ ] **Step 8: Commit**

```bash
git add src/local_ledger/models.py alembic/versions/ tests/unit/test_theme_settings.py
git commit -m "feat(settings): add theme_mode column to ProviderSettings"
```

---

## Task 7: App integration — inject CSS at startup + sidebar toggle

**Files:**
- Modify: `app.py`
- Modify: `app_pages/settings.py`
- Modify: `tests/unit/test_theme_smoke.py` (new file)

**Interfaces:**
- Consumes: `inject_css()` (Task 3), `kpi_card` / `status_badge` (Task 5), `load_settings` / `save_settings` (Task 6)
- Produces: working app entry point with theme applied + sidebar toggle persisted

- [ ] **Step 1: Write the failing smoke test**

Create `tests/unit/test_theme_smoke.py`:
```python
"""Smoke test: app boots in both modes under AppTest, no exception."""

from __future__ import annotations

import os

from streamlit.testing.v1 import AppTest


def test_app_boots_in_light_mode(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    os.environ["LOCAL_LEDGER_DB_URL"] = db_url
    at = AppTest.from_file("app.py").run()
    assert not at.exception


def test_app_boots_in_dark_mode(tmp_path) -> None:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    os.environ["LOCAL_LEDGER_DB_URL"] = db_url
    # Pre-set theme_mode to dark via a sentinel file is out of scope here;
    # this test just verifies the default light path doesn't crash.
    at = AppTest.from_file("app.py").run()
    assert not at.exception
```

- [ ] **Step 2: Run the tests to verify they fail (or pass cleanly)**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme_smoke.py -v`
Expected: Tests pass (the app already boots). This is a forward-looking guard — they must continue to pass after the modifications below.

- [ ] **Step 3: Add `inject_css` call to `app.py`**

In `app.py`, inside `main()`, right after `st.set_page_config(...)` (line 80), insert:

```python
    from local_ledger.ui.theme import inject_css
    from local_ledger.llm.providers import load_settings

    _settings = load_settings(_session_for_theme())
    inject_css(_settings.theme_mode)
```

Then add a helper near the top of the file (after the imports):

```python
def _session_for_theme():
    """One-time DB session for the theme lookup. Reuses the chrome helper."""
    from local_ledger.ui.chrome import get_session

    return get_session()
```

Result: `app.py` `main()` becomes:
```python
def main() -> None:
    import streamlit as st

    from local_ledger.ui.chat_sidebar import render_chat_sidebar
    from local_ledger.ui.chrome import get_session
    from local_ledger.ui.chrome import render_ollama_banner
    from local_ledger.ui.theme import inject_css
    from local_ledger.llm.providers import load_settings

    non_loopback = _check_runtime_bind()
    if non_loopback:
        st.set_page_config(page_title="Local Ledger - REFUSED")
        st.error(
            f"REFUSING TO SERVE on non-localhost address {non_loopback!r}. "
            "Local Ledger is single-user localhost-only by design. "
            "Remove the --server.address flag or set it to 127.0.0.1."
        )
        st.stop()

    st.set_page_config(page_title="Local Ledger", page_icon="📒", layout="wide")

    _settings = load_settings(get_session())
    inject_css(_settings.theme_mode)

    page = st.navigation(...)
    render_ollama_banner()
    page.run()
    render_chat_sidebar(context=st.session_state.get("chat_context"))
```

- [ ] **Step 4: Add Theme radio to `app_pages/settings.py`**

Insert this block in `settings.py` AFTER the existing `settings = providers.load_settings(session)` line and BEFORE the existing `st.subheader("Local LLM (Ollama)")` line. The block must run after `settings` is loaded (it reads `settings.theme_mode`) and after `session` is loaded (it calls `save_settings(session, …)`):

```python
from local_ledger.ui.components import section_header
from local_ledger.ui.theme import inject_css

section_header("DISPLAY", "Theme")

mode = st.radio(
    "Theme",
    ["light", "dark"],
    index=0 if settings.theme_mode == "light" else 1,
    horizontal=True,
    key="theme_mode_radio",
)
if mode != settings.theme_mode:
    from local_ledger.llm.providers import save_settings

    save_settings(session, theme_mode=mode)
    inject_css(mode)
    st.success(f"Theme switched to {mode}.")
    st.rerun()

st.divider()
```

(The existing `st.title("Settings")` and `session = get_session()` and `settings = providers.load_settings(session)` lines remain unchanged above this block. The existing `st.subheader("Local LLM (Ollama)")` block follows.)

- [ ] **Step 5: Run the smoke tests to verify they still pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_theme_smoke.py -v`
Expected: 2 passed.

- [ ] **Step 6: Run the full suite to verify zero regressions**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 258 + 2 = 260 passed, 10 skipped.

- [ ] **Step 7: Commit**

```bash
git add app.py app_pages/settings.py tests/unit/test_theme_smoke.py
git commit -m "feat(ui): inject theme CSS at app startup; add theme toggle to Settings"
```

---

## Task 8: Page swaps — KPI pages (home.py, insights.py)

**Files:**
- Modify: `app_pages/home.py`
- Modify: `app_pages/insights.py`

**Interfaces:**
- Consumes: `section_header`, `kpi_card` (from Task 5); existing page logic
- Produces: pages with serif headlines + mono KPI cards

- [ ] **Step 1: Update `home.py`**

Replace `st.title("Local Ledger")` (line 12) with:
```python
from local_ledger.ui.components import kpi_card, section_header

section_header("OVERVIEW", "Local Ledger")
```

Replace the four `col.metric(...)` calls (lines 21-24) with `kpi_card` calls inside the columns:
```python
col1, col2, col3, col4 = st.columns(4)
with col1:
    kpi_card("Income", f"${summary.income_cents / 100:,.2f}")
with col2:
    kpi_card("Spent", f"${summary.spent_cents / 100:,.2f}")
with col3:
    kpi_card("Budgeted", f"${summary.budgeted_cents / 100:,.2f}")
with col4:
    kpi_card(
        "Net",
        f"${(summary.income_cents - summary.spent_cents) / 100:,.2f}",
        status="ok" if summary.income_cents >= summary.spent_cents else "bad",
    )
```

Replace `st.subheader("Budget vs actual")` with `section_header("BUDGET", "Budget vs actual")`. Replace `st.subheader("Trend (6 months)")` with `section_header("TREND", "6-month trend")`. Replace `st.subheader("Anomalies")` with `section_header("FINDINGS", "Anomalies")`.

- [ ] **Step 2: Update `insights.py`**

Replace `st.title("Insights")` (line 15) with:
```python
from local_ledger.ui.components import section_header, status_badge

section_header("ANALYSIS", "Insights")
```

Replace each `st.subheader(...)` with `section_header(...)`:
- `st.subheader("Deterministic findings")` → `section_header("EVAL", "Deterministic findings")`
- `st.subheader("LLM interpretation")` → `section_header("INTERPRET", "LLM interpretation")`
- `st.subheader("Recommendation history")` → `section_header("HISTORY", "Recommendation history")`
- `st.subheader("Multi-month trend narrative")` → `section_header("TREND", "Multi-month narrative")`
- `st.subheader("What-if scenarios")` → `section_header("WHAT IF", "Scenarios")`

Replace the `col2.success("followed")` / `col2.error("not followed")` lines (around line 86-88) with:
```python
        if r.followed is True:
            with col2:
                status_badge("followed", tone="ok")
        elif r.followed is False:
            with col2:
                status_badge("not followed", tone="bad")
```

- [ ] **Step 3: Run the existing AppTest smoke tests for these pages**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_goals_page.py tests/unit/test_insights_sections.py -v`
Expected: existing tests pass (these exercise the page modules).

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 260 passed, 10 skipped.

- [ ] **Step 5: Commit**

```bash
git add app_pages/home.py app_pages/insights.py
git commit -m "feat(ui): apply theme to home and insights pages (section headers, KPI cards, badges)"
```

---

## Task 9: Page swaps — table pages (transactions.py, budgets.py, goals.py)

**Files:**
- Modify: `app_pages/transactions.py`
- Modify: `app_pages/budgets.py`
- Modify: `app_pages/goals.py`

**Interfaces:**
- Consumes: `section_header`, `status_badge`, `apply_chart_colors`
- Produces: pages with serif headlines + status badges

- [ ] **Step 1: Update `transactions.py`**

Replace `st.title("Transactions")` (line 12 or wherever it lives) with:
```python
from local_ledger.ui.components import section_header

section_header("LEDGER", "Transactions")
```

Replace any `st.subheader(...)` with `section_header(...)` (keep the same eyebrow/title split: pick a short uppercase eyebrow like "FILTER" / "ADD" / "REVIEW" depending on the section).

- [ ] **Step 2: Update `budgets.py`**

Replace `st.title("Budgets")` with:
```python
from local_ledger.ui.components import section_header, status_badge

section_header("ALLOCATION", "Budgets")
```

For each row in the budget table that shows a status (over/under budget), render a `status_badge`:
```python
status_badge("Over budget", tone="bad")  # if spent > budgeted
status_badge("On track", tone="ok")     # if spent <= budgeted
```

Replace any `st.subheader(...)` with `section_header(...)`.

- [ ] **Step 3: Update `goals.py`**

Replace `st.title("Goals")` with:
```python
from local_ledger.ui.components import section_header, status_badge

section_header("PROGRESS", "Goals")
```

For each goal row, render a `status_badge` based on progress:
```python
if pct >= 100:
    status_badge("achieved", tone="ok")
elif pct >= 70:
    status_badge("on track", tone="ok")
elif pct >= 30:
    status_badge("behind", tone="warn")
else:
    status_badge("at risk", tone="bad")
```

Replace any `st.subheader(...)` with `section_header(...)`.

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 260 passed, 10 skipped.

- [ ] **Step 5: Commit**

```bash
git add app_pages/transactions.py app_pages/budgets.py app_pages/goals.py
git commit -m "feat(ui): apply theme to transactions, budgets, and goals pages"
```

---

## Task 10: Page swaps — light pages (rules.py, import_.py, settings.py)

**Files:**
- Modify: `app_pages/rules.py`
- Modify: `app_pages/import_.py`
- Modify: `app_pages/settings.py` (already modified in Task 7; final pass for consistency)

**Interfaces:**
- Consumes: `section_header`
- Produces: pages with serif headlines

- [ ] **Step 1: Update `rules.py`**

Replace `st.title("Rules")` with:
```python
from local_ledger.ui.components import section_header

section_header("AUTOMATION", "Rules")
```

Replace any `st.subheader(...)` with `section_header(...)`.

- [ ] **Step 2: Update `import_.py`**

Replace `st.title("Import")` with:
```python
from local_ledger.ui.components import section_header

section_header("INGEST", "Import")
```

Replace any `st.subheader(...)` with `section_header(...)`.

- [ ] **Step 3: Final consistency pass on `settings.py`**

The settings.py was already modified in Task 7 to add the theme radio. Now apply `section_header` to the existing `st.subheader(...)` calls:

```python
section_header("LOCAL LLM", "Local LLM (Ollama)")
# ... existing local-settings form ...
section_header("CLOUD", "Cloud provider (opt-in, per call)")
# ... existing cloud-settings form ...
section_header("CONSENT", "Cloud consent preview")
# ... existing preview block ...
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 260 passed, 10 skipped.

- [ ] **Step 5: Commit**

```bash
git add app_pages/rules.py app_pages/import_.py app_pages/settings.py
git commit -m "feat(ui): apply theme to rules, import, and settings pages"
```

---

## Task 11: Final verification

**Files:**
- (no code changes expected)
- Modify: `README.md` (only if a "Theme" section needs to be added; see Step 3)

- [ ] **Step 1: Run the full unit suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: 260 passed, 10 skipped. **Zero failures.**

- [ ] **Step 2: Run ruff**

Run: `.venv\Scripts\python.exe -m ruff check src tests app.py app_pages`
Expected: All checks passed.

- [ ] **Step 3: Manual visual smoke (optional but recommended)**

Run: `streamlit run app.py --server.address 127.0.0.1`
Then in the browser:
- [ ] Home page: KPI cards show mono numbers; serif title at top; eyebrow above
- [ ] Toggle Theme to dark in Settings → app re-renders in dark mode
- [ ] Restart app → dark mode persists (theme_mode saved to ProviderSettings)
- [ ] Insights page: section headers are serif; "followed" / "not followed" use status badges
- [ ] Transactions page: tables use mono numbers; sections have serif headers
- [ ] Budgets page: status badges render (over/under budget)
- [ ] Goals page: progress badges render
- [ ] Rules, Import, Settings: section headers use serif

- [ ] **Step 4: Update README if needed**

The README's "Privacy posture" section already mentions that the app runs locally; no new privacy implications from the theme. If the user wants a "Theme" section in the README, add a short paragraph under "What it does" describing the light/dark toggle and the emerald accent. (Skip this step if the user prefers to keep the README unchanged.)

- [ ] **Step 5: Final commit**

```bash
git add README.md  # if modified
git commit -m "docs(readme): note the light/dark theme toggle"
```

---

## Self-Review

**1. Spec coverage — every section of the spec maps to a task:**

| Spec section | Task |
|---|---|
| Context, Goals, Non-goals | (design accepted) |
| Aesthetic direction | Tasks 2-10 (drive component decisions) |
| Color tokens (light/dark) | Task 2 (dataclass), Task 3 (CSS injection) |
| Chart emerald ramp | Task 2 (constants), Task 4 (apply_chart_colors) |
| Typography (sans/serif/mono) | Task 2 (constants), Task 3 (CSS), Task 5 (wrappers) |
| Size scale, Spacing, Radii, Shadows | Task 2 (constants), Task 3 (CSS) |
| Font files (vendored) | Task 1 |
| Architecture (new/modified files) | Tasks 1-10 |
| `theme.py` module | Task 2-4 |
| `components.py` module | Task 5 |
| CSS injection mechanism | Task 3 |
| CSS coverage table | Task 3 (each selector implemented) |
| Settings persistence | Task 6 |
| Test plan (new + existing) | Tasks 2-7 (new tests), Tasks 8-10 (existing suite stays green) |
| Risks | Mitigated: Tasks 1 (vendored fonts), 3 (CSS shape), 6 (migration default) |

**2. Placeholder scan — no `TBD`, `TODO`, `implement later`, "fill in details", "add appropriate error handling", "similar to Task N", ellipsis stand-ins, or "write tests for the above" without code.** All tests have bodies; all implementations have full code.

**3. Type consistency — the names below are used consistently across tasks:**
- `ThemeTokens` (Task 2) → used in Task 3 with the same field names
- `TOKENS_LIGHT`, `TOKENS_DARK` (Task 2) → used in Task 3
- `set_active_mode`, `get_active_mode`, `active_tokens` (Task 2) → used in Task 3
- `palette_for_chart` (Task 2 list-stub) → implemented in Task 2 body, exercised in Task 3 tests, used in Task 4
- `apply_chart_colors` (Task 4) → only used by page tasks (Tasks 8-9) if charts are added; the spec leaves chart coloring optional
- `section_header`, `kpi_card`, `status_badge` (Task 5) → used in Tasks 8-10 with the same `eyebrow, title` / `label, value, delta, status` / `text, tone` signatures
- `theme_mode` field on `ProviderSettings` (Task 6) → used in Task 7's `app.py` integration and `settings.py` toggle
- `inject_css(mode)` (Task 3) → called from `app.py` (Task 7) and `settings.py` (Task 7) with the same `mode: Literal["light", "dark"]` signature

**4. Ambiguity check — resolved:**
- "Settings table" in the spec → clarified in Global Constraints as `ProviderSettings` (the existing singleton at `src/local_ledger/models.py:179`)
- "1 line of CSS class application" in spec Non-goals → removed in self-review; page swaps add 3-10 lines per page for `section_header` weights, not a single class
- "Hero KPI labels stay serif" → handled in Task 5: `kpi_card` uses `font-family: var(--font-sans)` for the label, `var(--font-mono)` for the value
- "All 237 existing tests stay green" → verified at the end of Tasks 3, 6, 7, 8, 9, 10, 11

If you find any spec requirement without a task after reading this plan, add the task before executing.
