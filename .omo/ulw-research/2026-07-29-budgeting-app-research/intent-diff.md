# Intent Diff: Budgeting App Research

## Expected Truths (from user intent)

| intent_id | expected truth | source | status |
|-----------|---------------|--------|--------|
| IT-1 | Open-source budgeting apps (Actual Budget, Firefly III, GnuCash) implement transaction import without paid aggregation APIs | User spec: "self-hosted/local-first app without paid aggregation APIs" | pending |
| IT-2 | Multiple import formats are realistic for self-hosted: CSV bank-statement, OFX/QFX, manual entry | User spec | pending |
| IT-3 | A meaningful distinction exists between double-entry ledger and simple-ledger models in OSS budgeting apps | User spec: "double-entry vs simple ledger" | pending |
| IT-4 | Envelope budgeting (YNAB-style) and category budgeting (Mint-style) are distinguishable design patterns in OSS apps | User spec | pending |
| IT-5 | Open-source apps implement recurring transaction detection/automation | User spec | pending |
| IT-6 | Month-over-month trend computation is a documented feature in at least one major OSS budgeting app | User spec | pending |
| IT-7 | At least one open-source project (Actual Budget AI plugin, Firefly III integration, or other) implements LLM-driven analysis of a ledger | User spec | pending |
| IT-8 | Each major claim should be backed by 2+ independent sources (user verification rule) | User spec | pending |

## Violated Invariants
(none yet)

## Linked claim ids
(pending; populated as observations land)
