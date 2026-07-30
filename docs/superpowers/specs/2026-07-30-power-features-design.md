# Wave 3: Power Features & Automation — Design Doc

**Date:** 2026-07-30
**Status:** Approved direction (single wave, all three features)
**Depends on:** Hardening complete (commit history through wave 2)

## Scope

Three features sharing transaction-scanning infrastructure:

1. **Recurring transaction detection** — detect subscriptions/bills by grouping similar payees + amounts at regular intervals
2. **Rules engine** — user-defined rules (payee/amount/notes → category) that auto-categorize on import and backfill existing transactions
3. **Filtering, bulk edit & charts** — amount range + text search filters, bulk select → delete/categorize, category pie chart + trend time series

## Out of scope (this wave)

- PDF/OFX import (wave 4)
- Investment/portfolio tracking (wave 4)
- Scheduled/automated evaluation runs (separate feature)
- Recurring transaction auto-creation (detection only, no creation)

## Architecture

```
automation/
  recurring.py    — detect recurring transaction groups
  rules.py        — rule engine: create/apply rules to transactions

Transactions page enhancements:
  — amount range + text search filters
  — bulk select → delete / categorize
  — category breakdown pie chart + trend time series
```

## Components

### 1. `automation/recurring.py` — Recurring detection

- `detect_recurrences(transactions, tolerance_pct=10) -> list[RecurringGroup]`
- Groups by normalized payee, checks for amount similarity within tolerance, flags regular intervals
- `RecurringGroup(payee, amount_cents, interval_days, transaction_ids, confidence)`
- Pure computation, no LLM (test-enforced)
- Normalization reuses existing `normalize_payee` pattern from `dedup.py`

### 2. `automation/rules.py` — Rules engine

- New `Rule` model: `name, payee_contains, amount_min, amount_max, notes_contains, category_id, priority, created_at`
- `RuleService(session)`: CRUD + `apply_rules(transaction, rules) -> category_id | None` (first match wins) + `apply_rules_to_uncategorized(session, rules)`
- Rules UI: create/edit/delete on a dedicated page or expander
- Integration: rules applied after dedup, before LLM categorization (cheaper + deterministic first)

### 3. Transactions page enhancements

- Filters: amount range slider, text search (payee/notes), category multi-select
- Bulk: checkbox select → bulk delete or bulk assign category
- Charts: category breakdown pie + monthly trend line

## Data flow

```
Import → dedup → rules.apply_rules() → (if uncategorized) → LLM categorize
Recurring detection runs on demand (button) or after import
Rules CRUD on a dedicated "Rules" page or expander
```

## Error handling

- Rule with unknown category_id → skip (don't crash)
- Recurring detection with <2 transactions → empty list
- Rules applied to 0 transactions → notice, not error
- Bulk delete with selection → confirm dialog

## Privacy

- Recurring detection and rules are pure local computation (no LLM, no cloud)
- No new data leaves the machine

## Testing strategy

- **recurring.py**: grouping math, tolerance edges, interval detection, no-LLM-imports invariant
- **rules.py**: match priority, partial match, backfill, unknown-category skip
- **UI (AppTest)**: transactions page renders with filters, bulk select works, charts render
- **Invariants**: grep — no LLM imports in automation/

## Effort estimate

~8-10 todos: 2 automation modules + Rule model + transactions page enhancements + tests.
