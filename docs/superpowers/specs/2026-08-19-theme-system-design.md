# Theme System — Design

**Date:** 2026-08-19
**Status:** Self-review complete → awaiting user review
**Scope:** Whole-app cohesive theme across all 7 pages + light/dark toggle

---

## Context

The current Streamlit app uses Streamlit defaults — no custom theme, no design tokens, no dark mode. The visual surface is inconsistent across pages (charts, tables, KPI cards each render with Streamlit's defaults). The user requested a cohesive theme system that makes the app feel like a real product, references Jane Street's internal-tool aesthetic (information-dense, keyboard-first, functional), and supports both light and dark mode.

Out of scope: bank sync, multi-user, MCP/API, multi-currency, investments — all remain scope-OUT.

## Goals

- One source of truth for all visual decisions (tokens in Python, generated CSS)
- Consistent appearance across all 7 pages, charts, tables, forms
- Light + dark mode with a single persisted toggle (no extra knobs)
- Headlines and eyebrows get a serif personality (Alegreya); body and labels stay sans-serif for fast reading; numbers stay monospace for column alignment
- Offline-first preserved — no external CDN, no online-only first-load
- Zero behavioral change to service layer; all existing 237 unit tests stay green

## Non-goals

- Custom HTML components / JS shims
- External font fetches at runtime
- Animated transitions / motion design
- WCAG AAA audit (focus ring only; full audit deferred to a separate thread)
- Component-level refactor of page logic (page files only get header/kpi/badge swaps + 1 line of CSS class application)
- Mobile-specific layout (Streamlit default responsive behavior is preserved)

## Aesthetic direction

Inspired by Jane Street's internal Bonsai-built tools: high information density, keyboard-first, strong typography hierarchy, calm color. Numbers are prominent and tabular. Chrome fades to the background.

- **Layout:** generous gutters between sections, minimal in-section padding, content-first
- **Numbers:** mono family + `font-variant-numeric: tabular-nums` for alignment
- **Hierarchy:** size + weight + color, never weight alone
- **Decoration:** subtle borders, soft shadows, no gradients, no glow

## Design tokens

### Color — Light mode

| Token | Hex | Use |
|---|---|---|
| `bg` | `#FAFAF7` | Page background (warm off-white) |
| `surface` | `#FFFFFF` | Cards, panels, tables |
| `ink` | `#0F0F10` | Primary text |
| `ink_muted` | `#5C5C5C` | Secondary text, metadata |
| `border` | `#E5E5E1` | Card borders, dividers |
| `border_subtle` | `#EFEFEC` | Table row hover, table grid |
| `accent` | `#0F766E` | Primary emerald (teal-700) |
| `accent_hover` | `#115E59` | Hover/active (teal-800) |
| `accent_soft` | `#CCFBF1` | Tinted backgrounds, selected rows (teal-100) |
| `success` | `#059669` | Under budget, positive |
| `warning` | `#D97706` | Near budget |
| `danger` | `#DC2626` | Over budget, errors |

### Color — Dark mode

| Token | Hex | Use |
|---|---|---|
| `bg` | `#0E0E10` | Page background |
| `surface` | `#1A1A1F` | Cards, panels, tables |
| `ink` | `#F5F5F2` | Primary text |
| `ink_muted` | `#A1A1A1` | Secondary text, metadata |
| `border` | `#2A2A2E` | Card borders, dividers |
| `border_subtle` | `#1F1F22` | Table row hover, table grid |
| `accent` | `#14B8A6` | Primary emerald (teal-500, brighter) |
| `accent_hover` | `#2DD4BF` | Hover/active (teal-400) |
| `accent_soft` | `#1A2E2C` | Tinted backgrounds, selected rows |
| `success` | `#10B981` | Under budget, positive |
| `warning` | `#F59E0B` | Near budget |
| `danger` | `#F87171` | Over budget, errors |

### Chart emerald ramp (5-step sequential)

| Mode | Light → Dark |
|---|---|
| Light | `#CCFBF1` → `#99F6E4` → `#5EEAD4` → `#2DD4BF` → `#14B8A6` |
| Dark | `#5EEAD4` → `#2DD4BF` → `#14B8A6` → `#0D9488` → `#0F766E` |

Plus a single neutral gray (`#A1A1A1` light / `#5C5C5C` dark) for "uncategorized" so it doesn't compete with the emerald sequence.

### Typography

**Sans-serif family** (body, controls, labels, KPI labels):
```
system-ui, -apple-system, "Segoe UI", Roboto, Inter, sans-serif
```

**Serif family** (page titles, section headers, eyebrows, hero KPI labels):
```
"Alegreya", "Alegreya Sans", Georgia, "Times New Roman", serif
```

**Mono family** (amounts in tables, hero KPI values, code):
```
ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace
```

### Size scale (rem, base 16px)

| Token | Rem | Use |
|---|---|---|
| `xs` | 0.75 | Eyebrows, metadata, badges |
| `sm` | 0.875 | Dense table cells, secondary |
| `base` | 1.0 | Body text |
| `lg` | 1.125 | Subheads |
| `xl` | 1.25 | Section headers |
| `2xl` | 1.5 | Hero KPIs |
| `3xl` | 2.0 | Page titles |

**Weights:** 400 body, 500 emphasis, 600 titles. No 700.
**Numbers:** `font-variant-numeric: tabular-nums` on all amount cells in tables.
**Hero KPI values:** mono + size `2xl` + weight 500. (Hero KPI *labels* stay sans-serif at `xs` so the value is the only loud element on the card.)

### Spacing

Eight-step scale (px): `4, 8, 12, 16, 24, 32, 48, 64`. Each step is roughly 1.5× the previous (4-pt grid with practical breakpoints).

| Use | Value |
|---|---|
| Section gutter | 24px |
| Page margin | 32px |
| Card padding | 16px |
| Form field gap | 12px |

### Radii

| Token | Value | Use |
|---|---|---|
| `sm` | 6px | Buttons, inputs, badges |
| `md` | 10px | Cards, panels |
| `lg` | 16px | Modals |

### Shadows

| Token | Light | Dark |
|---|---|---|
| `card` | `0 1px 2px rgba(0,0,0,.04), 0 2px 8px rgba(0,0,0,.04)` | `0 1px 2px rgba(0,0,0,.4), 0 2px 8px rgba(0,0,0,.3)` |

That's the only shadow. No glow, no dramatic elevation.

### Font files (vendored)

Two .woff2 files committed to `src/local_ledger/ui/fonts/`:

- `alegreya-regular.woff2` — Regular (400)
- `alegreya-bold.woff2` — Bold (700)

Loaded via `@font-face` in the generated CSS. Total ~600 KB. License: SIL Open Font License (free, no attribution required in app, but documented in `fonts/LICENSE.txt`).

## Architecture

### New files

```
src/local_ledger/ui/
  theme.py        # tokens, inject_css, palette_for_chart, apply_chart_colors
  components.py   # section_header, kpi_card, status_badge
  fonts/
    LICENSE.txt
    alegreya-regular.woff2
    alegreya-bold.woff2

tests/unit/
  test_theme.py
  test_components.py

alembic/versions/
  YYYYMMDD_HHMM_theme_mode.py
```

### Modified files

```
.streamlit/config.toml                          # minimal native theme
src/local_ledger/models.py                       # theme_mode field on Settings
app.py                                           # inject_css call + sidebar toggle
app_pages/home.py                                # section_header + kpi_card
app_pages/insights.py                            # section_header + kpi_card + status_badge
app_pages/transactions.py                        # section_header
app_pages/budgets.py                             # section_header + status_badge
app_pages/goals.py                               # section_header + status_badge
app_pages/rules.py                               # section_header
app_pages/import_.py                             # section_header
app_pages/settings.py                            # theme radio + save
```

### Module: `src/local_ledger/ui/theme.py`

```python
@dataclass(frozen=True)
class ThemeTokens:
    # Colors
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
    # Sizes (rem)
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

TOKENS_LIGHT: ThemeTokens = ThemeTokens(...)  # values from the Light-mode table above
TOKENS_DARK: ThemeTokens = ThemeTokens(...)   # values from the Dark-mode table above

def inject_css(mode: Literal["light", "dark"]) -> None:
    """Emit one <style> block derived from active tokens.
    Uses CSS variables on :root so the whole sheet is one block.
    Called once at app startup + on every mode toggle."""

def palette_for_chart() -> list[str]:
    """Return the 5-step emerald ramp for the current mode."""

def apply_chart_colors(chart_call_fn, *args, **kwargs) -> None:
    """Wrap a chart call so it picks up palette_for_chart() via color=."""
```

### Module: `src/local_ledger/ui/components.py`

Three thin wrappers:

1. **`section_header(eyebrow: str, title: str)`** — small uppercase `eyebrow` (serif, `xs`, `ink_muted`) above an `xl` weight-600 `title` (serif, `ink`). Used on every page above the first content block.
2. **`kpi_card(label: str, value: str, delta: str | None = None, status: Literal["ok", "warn", "bad"] | None = None)`** — replaces raw `st.metric` calls on Home and Insights. `label` is sans-serif `xs` `ink_muted`; `value` is mono `2xl` weight 500; `delta` (if present) is colored by `status` via `success`/`warning`/`danger`. Reuses the `st.metric` DOM but standardizes the typography stack.
3. **`status_badge(text: str, tone: Literal["ok", "warn", "bad", "neutral"])`** — small inline pill (sans-serif `xs`, radius `sm`) used for budget status, goal progress, rule status, transaction review tags. Tinted background per tone via `accent_soft` / warning-soft / danger-soft / neutral.

Everything else is targeted by CSS.

### CSS injection mechanism

`inject_css("light")` (or `"dark"`) emits a single `<style>` block via `st.markdown(..., unsafe_allow_html=True)`. The block defines:

1. `@font-face` rules for the two vendored Alegreya files
2. CSS custom properties on `:root` (one per token)
3. Selector rules for each surface listed below

The generated CSS is a single function — no per-page CSS, no scattered `st.markdown` calls.

### CSS coverage (selectors targeted)

Selectors below are Streamlit-internal `data-testid` values. They are stable across recent 1.x releases but may change without notice; if a future Streamlit upgrade breaks a selector, expect silent visual regression (covered by the `Risks` section).

| Surface | Selector | What changes |
|---|---|---|
| Root | `.stApp`, `header[data-testid="stHeader"]` | bg, ink, base font |
| Sidebar | `[data-testid="stSidebar"]` | bg, right border, section spacing |
| Cards/panels | `[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"]` (containers above second-level) | bg = surface, border, radius, shadow |
| Tables | `[data-testid="stDataFrame"]`, `[data-testid="stDataEditor"]` | tabular-nums, header weight 500, border colors, row hover |
| Forms | `[data-testid="stTextInput"] input`, `[data-testid="stNumberInput"] input`, `[data-baseweb="select"]`, `[data-testid="stTextArea"] textarea`, `[data-testid="stDateInput"] input` | border, radius, focus ring |
| Buttons | `button[kind="primary"]`, `button[kind="secondary"]` (best-effort — fallback to targeting `stButton button`) | primary = accent + white ink; secondary = surface + ink; no gradients |
| Metrics | `[data-testid="stMetric"]` | label = ink_muted xs uppercase; value = mono 2xl 500; delta colored via success/danger |
| Alerts | `[data-testid="stAlert"]` | soft tinted bg matching the variant |
| Charts | `.js-plotly-plot` | color via Plotly `color=` param (set by `apply_chart_colors`) |
| Tabs | `[data-baseweb="tab"]` | underline accent on active, ink_muted on inactive |
| Code | `[data-testid="stCode"]` | mono, surface bg, 1px border |
| Focus | `*:focus-visible` | 2px solid accent + 2px offset |

## Settings persistence

Add `theme_mode: str` column to the existing `Settings` table (default `"light"`). Alembic migration autogenerated.

App startup reads `settings.theme_mode` and calls `inject_css(mode)` before any other `st` call. Sidebar exposes a `st.radio("Theme", ["light", "dark"], ...)` — on change, writes to Settings and `st.rerun()`.

No new top-level nav, no separate page, no extra knobs.

## Test plan

**New unit tests:**

- `tests/unit/test_theme.py`
  - Token invariants: required keys present; light/dark cover same keys
  - Mono tone arithmetic: dark ramp is monotonically darker; light ramp is monotonically lighter
  - `inject_css("light")` and `inject_css("dark")` both produce parseable CSS (no orphan brace, valid selectors)
  - `palette_for_chart()` returns 5 colors and they cover the emerald sequential in both modes
  - `@font-face` URLs reference the vendored files (no external CDN)

- `tests/unit/test_components.py`
  - `section_header` renders with correct eyebrow + title text
  - `kpi_card` renders with correct label/value and the expected status class
  - `status_badge` renders with correct text and tone class
  - All three render under `AppTest` against a temp DB with `LOCAL_LEDGER_DB_URL` set

**Existing tests:**
- All 237 unit tests stay green (no behavior change to service layer, page files, or DB).

**Visual smoke (manual):**
- `streamlit run app.py` → screenshot Home, Insights, Transactions, Budgets in both modes
- Toggle persistence: switch to dark, restart app, confirm dark mode loads
- Font check: page titles render in Alegreya serif; body in sans-serif; numbers in mono

## Out of scope (deferred)

- Custom Plotly themes (we use the `color=` parameter; the rest of Plotly stays default)
- Font subsetting / variable fonts
- Animated transitions / motion
- Full WCAG accessibility audit (focus ring only; deeper audit is a separate thread)
- `surface_elevated` / `ink_faint` tokens (not needed yet; add if disabled states or tertiary text show up)
- Mobile-specific layout tweaks (Streamlit default responsive behavior is preserved)

## Risks

- **Streamlit selector stability:** Streamlit occasionally changes internal `data-testid` values. If a selector stops matching after an upgrade, the relevant rule fails silently. Mitigation: one `test_theme.py` case that asserts CSS contains the expected selectors; a manual visual smoke catches the rest. README upgrade notes call this out.
- **Alegreya at small sizes:** the serif is used at `xs` (eyebrows), `xl` (section headers), and `3xl` (page titles). Below `xs` (i.e. `sm` and below in the size scale), body falls back to sans-serif so dense table cells stay legible. The `xs` eyebrow is the deliberately-serif surface; the rest is sans.
- **Plotly color parameter:** passing `color=` to `st.bar_chart` / `st.line_chart` requires the value to be a column name or a hex list. Multi-series charts need `color=` per series. Mitigation: `apply_chart_colors` helper centralizes the convention.
