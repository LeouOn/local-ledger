# Phase 1: LLM Evaluation Engine — Design Doc

**Date:** 2026-07-29
**Status:** Approved direction (two-phase build; LLM features first)
**Depends on:** v1 complete (30 todos, commit 8ce05a4)

## Scope

Two features sharing one projection engine:

1. **Multi-month trend narratives** — the LLM interprets 3–12 months of aggregate data and writes a narrative analysis (rising categories, projected trajectory, warnings).
2. **What-if scenario modeling** — interactive per-category sliders that recompute projected outcomes instantly; optional LLM explanation of the impact.

## Out of scope (this phase)

- Savings goal tracking (Phase 2)
- Conversational Q&A / chat sidebar (Phase 2)
- Any new input formats (OFX, PDF, etc.)
- Raw transactions in any LLM prompt (privacy invariant, unchanged)

## Architecture

```
MonthSummary (trends.py, existing) × N months
  → MultiMonthPayload (aggregates, token-capped, no raw transactions)
  → LLM interpretation (narrative.py)
  → TrendNarrative (typed output)
  → Stored as Recommendation (append-only, existing pattern)

User slider input (Insights page)
  → Scenario engine (scenarios.py, pure computation)
  → ScenarioResult / ProjectedMonth (before/after)
  → Optional LLM explanation
  → Displayed inline
```

**Invariants carried over from v1:**

- No LLM in the arithmetic path — all numbers computed in Python from integer-cents aggregates.
- Aggregates-only for cloud; full aggregate context for local.
- All LLM calls are explicit user actions (buttons), never on page load.

## Components

### 1. `src/local_ledger/analysis/scenarios.py` — Scenario/projection engine

Pure computation module. No LLM imports (test-enforced).

- `what_if_scenario(base: MonthSummary, category_id: int, pct_change: float) -> ScenarioResult`
  - Applies a percentage change to a category's spent amount for a given month.
  - Returns before/after: original spent, adjusted spent, savings delta in cents, projected year-end balance delta (= savings delta × months remaining in the current year).
  - `pct_change = -0.20` means "spend 20% less".
- `project_forward(summaries: list[MonthSummary], months_ahead: int, adjustments: dict[int, float]) -> list[ProjectedMonth]`
  - Baseline: per-category average spent across ALL provided summary months (user selects 3/6/12).
  - Returns a list of projected months with per-category projected spent and cumulative savings.
- Types: `ScenarioResult` (before/after), `ProjectedMonth` (month label, projected totals).

### 2. `src/local_ledger/analysis/multimonth.py` — Multi-month payload builder

- `build_multimonth_payload(months: list[str], summaries: list[MonthSummary]) -> MultiMonthPayload`
  - Collapses N months of per-category totals into a compact typed payload.
  - Per category: monthly spent array, monthly income array, MoM delta %, trend direction (`up`/`down`/`flat`; flat = |MoM delta| < 10%).
  - Token-capped at ≤4096 tokens (tiktoken, same as v1 payloads.py).
  - Truncates oldest months first if over cap; sets `truncated=True`.
- `to_prompt(payload: MultiMonthPayload) -> str` — JSON-rendered prompt text.

### 3. `src/local_ledger/analysis/narrative.py` — Trend narrative evaluation

- `generate_trend_narrative(payload: MultiMonthPayload, *, completer, model: str, provider: str = "local", consent_token: str | None = None, settings=None) -> TrendNarrative`
  - Calls the LLM via structured extraction (existing `structured.extract`).
  - Returns typed `TrendNarrative`: `summary`, `key_trends: list[str]`, `projections: list[str]`, `warnings: list[str]`.
  - Cloud path requires consent token + settings (same pattern as `evaluate.llm_interpret`).
- Narratives are stored as `Recommendation` rows (append-only, existing pattern) with `provider` and `model` recorded.

### 4. UI: `app_pages/insights.py` additions

- **Trend narrative section**: month-range selector (3/6/12 months), "Generate trend narrative" button, narrative cards (summary, key trends, warnings).
- **What-if scenario section**: per-category sliders (−50% to +50%), instant before/after comparison (original spent vs adjusted spent, savings delta), optional "Explain this scenario" button for LLM narrative.

## Data flow

### Trend narrative

```
User selects 6 months → clicks "Generate trend narrative"
  → trends.month_summary() called for each of 6 months
  → multimonth.build_multimonth_payload(months, summaries)
  → narrative.generate_trend_narrative(payload, completer, model)
  → TrendNarrative displayed as cards
  → Saved as Recommendation (month = latest month in range)
```

### What-if scenario

```
User moves dining slider to -20%
  → scenarios.what_if_scenario(current_month_summary, dining_id, -0.20)
  → ScenarioResult computed instantly (no LLM call)
  → Before/after displayed: "Dining: $400/mo → $320/mo, saves $80/mo = $960/yr"
  → Optional: "Explain" button → LLM narrates the impact
```

## Error handling

- LLM unavailable (Ollama not running): degradation banner (existing `ui/chrome.py`), button disabled, deterministic projections still work.
- Scenario with zero-value category: slider shows "No spending in this category" instead of 0/0 division.
- Multi-month payload over token cap: oldest months truncated, `truncated=True` flag shown to user.
- Invalid month range (start > end): clamped to available data with a notice.

## Testing strategy

TDD throughout (same as v1):

- **scenarios.py**: pure-math tests — cut 20% → 80% of original; project_forward arithmetic; zero-spend category edge case.
- **multimonth.py**: payload structure, token cap (10K-tx ledger still ≤4096), truncation flag.
- **narrative.py**: fake completer returns valid narrative JSON → typed output; garbage JSON → StructuredOutputError; cloud path blocked without consent.
- **UI (AppTest)**: Insights page renders new sections; slider interaction triggers scenario recompute; no LLM call on page load.
- **Invariants**: grep tests — no LLM imports in scenarios.py; no raw transaction fields in multimonth payload.

## Effort estimate

~8 todos: 3 engine modules (scenarios, multimonth, narrative) + 2 UI sections + 3 test suites.
