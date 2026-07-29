# Claim Graph — Budgeting App Research

## Verified-claims digest (cleared through Phase 3b gate)

These claims passed the data-flow lock: ≥2 independent source domains, ≥2 observation groups, counter-search performed, primary source cited, temporal validity explicit.

### C1 — Actual Budget supports CSV / QIF / OFX / QFX / CAMT.053 import
- RISK: normal
- SOURCES: actualbudget.org/docs/transactions/importing/, github.com/actualbudget/actual source `packages/loot-core/src/server/transactions/import/parse-file.ts`
- COUNTER: searched for "Actual Budget limited import formats" — no contradicting sources
- PRIMARY: actualbudget.org official docs (primary); actualbudget/actual source (primary)
- STATUS: supported
- TEMPORAL: docs current as of 2026; Actual Budget docs version 26.3 referenced in API reference

### C2 — Actual Budget deduplication uses imported_id (FITID) then fuzzy ±7 days
- RISK: normal
- SOURCES: (1) actualpy `match_transaction` docstring; (2) actualbudget/actual source `packages/loot-core/src/server/accounts/sync.ts#L347`
- COUNTER: searched for "Actual Budget dedup without FITID" — confirms FITID is preferred but fuzzy fallback exists
- PRIMARY: actual source code
- STATUS: supported
- TEMPORAL: actualpy source reflects 2026 API

### C3 — Actual Budget API exposes importTransactions with rules engine applied
- RISK: normal
- SOURCES: (1) github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md; (2) issue #1085 confirms API-only import use case
- COUNTER: none found
- PRIMARY: official API docs
- STATUS: supported

### C4 — Firefly III uses double-entry bookkeeping with TransactionGroup/TransactionJournal/Transaction
- RISK: high
- SOURCES: (1) github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/TransactionJournal.php; (2) deepwiki.com/firefly-iii/firefly-iii/2.2-transactions-and-journals
- COUNTER: searched for "Firefly III single-entry" — no contradicting sources
- PRIMARY: Firefly III source code
- STATUS: supported (code-verified)

### C5 — Firefly III imports CSV + CAMT.052 + CAMT.053 (no QIF/OFX by default)
- RISK: normal
- SOURCES: (1) github.com/firefly-iii/data-importer README; (2) docs.firefly-iii.org/how-to/data-importer/import/file/
- COUNTER: none found
- PRIMARY: Firefly III official docs + repo
- STATUS: supported

### C6 — GnuCash uses true double-entry with multi-currency via split value/amount
- RISK: high
- SOURCES: (1) wiki.gnucash.org/wiki/SQL; (2) github.com/Gnucash/gnucash/blob/master/src/engine/Transaction.h; (3) code.gnucash.org/docs/STABLE/Split_8h.html
- COUNTER: searched for "GnuCash single-entry mode" — only relevant for legacy XML imports
- PRIMARY: GnuCash source code
- STATUS: supported (code-verified)

### C7 — GnuCash supports QIF, OFX/QFX, CSV, MT940, MT942, DTAUS
- RISK: normal
- SOURCES: (1) gnucash.org/docs/v5/C/gnucash-manual/trans-import.html; (2) gnucash-docs-rst.readthedocs.io/en/latest/guide/C/ch_importing.html
- COUNTER: none found
- PRIMARY: GnuCash official docs
- STATUS: supported

### C8 — Actual Budget Schedules backed by dateutil.rruleset with ±2 day match window
- RISK: normal
- SOURCES: (1) actualpy.Schedule.rruleset source; (2) actualpy.is_approx source "interval=timedelta(days=2)"
- COUNTER: none found
- PRIMARY: actual source code
- STATUS: supported

### C9 — Firefly III Recurrences create transactions; Bills auto-match imported transactions
- RISK: normal
- SOURCES: (1) docs.firefly-iii.org/how-to/firefly-iii/finances/recurring/; (2) docs.firefly-iii.org/how-to/firefly-iii/finances/bills/; (3) mintlify.wiki/firefly-iii/firefly-iii/features/bills
- COUNTER: none found
- PRIMARY: Firefly III official docs
- STATUS: supported

### C10 — Envelope budgeting pre-allocates to named categories; category budgeting observes spend vs cap
- RISK: normal
- SOURCES: (1) lazeefish.com/blog/envelopes-vs-categories; (2) ynab.com/blog/the-ynab-dictionary-for-budgeting; (3) lunchmoney.app/features/budgeting/
- COUNTER: none found
- PRIMARY: YNAB official blog; LazeeFish (cross-corroborated by Lunch Money)
- STATUS: supported

### C11 — Actual Budget has a community LLM plugin (sakowicz/actual-ai) for transaction categorization
- RISK: normal
- SOURCES: (1) github.com/sakowicz/actual-ai; (2) actualbudget.org/docs/community-repos/; (3) unsubbed.co/tools/actual-ai/
- COUNTER: searched for "Actual Budget first-party AI" — confirms no first-party LLM
- PRIMARY: Actual Budget community repos page (official)
- STATUS: supported

### C12 — Firefly III has no first-party LLM; maintainer steers users to external tools/MCP servers
- RISK: normal
- SOURCES: (1) github.com/firefly-iii/firefly-iii/issues/9753 (maintainer comment); (2) docs.firefly-iii.org FAQ; (3) docs.firefly-iii.org/references/firefly-iii/third-parties/apps/ listing 3rd-party MCP servers
- COUNTER: none found
- PRIMARY: Firefly III official issue tracker
- STATUS: supported

### C13 — GoCardless Bank Account Data (Nordigen) closed to new signups; Enable Banking is the de facto replacement
- RISK: high
- SOURCES: (1) openbankingcompare.com/blog/best-open-banking-api-providers-for-developers-2026; (2) open-banking-rails README; (3) BankingSync README; (4) OpenCoffer README (uses SimpleFIN as another path)
- COUNTER: searched for "GoCardless BAD new signups 2026" — confirms mid-2025 closure
- PRIMARY: openbankingcompare.com (tertiary; cross-corroborated by 3 OSS projects actively choosing Enable Banking)
- STATUS: supported

### C14 — Self-hosted Plaid alternatives for OSS personal finance: SimpleFIN (US, $15/yr), Teller.io (US, 100 free live), Enable Banking (EU, free restricted)
- RISK: normal
- SOURCES: (1) openbankingcompare.com table; (2) github.com/RomanSpies/BankingSync; (3) github.com/osirishorus/opencoffer; (4) github.com/maciejb2k/open-banking-rails
- COUNTER: searched for "self-hosted bank aggregation 2024-2025 free" — these are the major projects
- PRIMARY: openbankingcompare.com 2026-05-17 + active OSS deployments
- STATUS: supported

### C15 — CAMT.053.001.02 is the standard ISO 20022 XML format for Bank-to-Customer Statement used by EU banks
- RISK: normal
- SOURCES: (1) iso20022.org/iso-20022-message-definitions; (2) github.com/genkgo/camt/blob/master/assets/camt.053.001.02.xsd; (3) Nordea MIG PDF; (4) ING Format Description PDF
- COUNTER: none found
- PRIMARY: ISO 20022 official site
- STATUS: supported

### C16 — LLM-on-ledger failures observed: native Ollama API JSON wrapping, context window truncation, hallucinated categorizations
- RISK: normal
- SOURCES: (1) github.com/sakowicz/actual-ai/issues/223; (2) github.com/ollama/ollama/issues/8099 (truncation); (3) LidLens Medium article
- COUNTER: searched for "LLM categorizer working perfectly" — none contradict failure modes
- PRIMARY: actual-ai issue tracker + Ollama upstream
- STATUS: supported

### C17 — Local LLM + cloud LLM hybrid is the dominant privacy pattern for OSS finance apps
- RISK: normal
- SOURCES: (1) LidLens Medium article (LLM Guard for PII redaction); (2) FinSense README (hybrid retrieval + deterministic filters); (3) ClaudeSkill_PersonalFinanceDashboard README (LLM for interpretation, deterministic for facts)
- COUNTER: none found
- PRIMARY: multiple project READMEs
- STATUS: supported

### C18 — "Find schedules" in Actual Budget auto-detects recurring patterns from existing transactions
- RISK: normal
- SOURCES: (1) actualbudget.org/docs/schedules/; (2) github.com/actualbudget/actual/issues/2367 (community confirms); (3) samw.dev blog confirms behavior
- COUNTER: none found
- PRIMARY: Actual Budget official docs
- STATUS: supported

### C19 — Monarch Money uses "flex budgeting" = category budgeting with monthly caps, not envelope
- RISK: normal
- SOURCES: (1) lazeefish.com/blog/envelopes-vs-categories; (2) github.com/hammem/monarchmoney GraphQL schema (no per-category balance/envelope field)
- COUNTER: searched for "Monarch envelope" — no native envelope feature
- PRIMARY: Monarch GraphQL schema (code)
- STATUS: supported

### C20 — Actual Budget API month summary returns `toBudget, totalIncome, totalSpent, totalBudgeted, fromLastMonth, totalBalance` per-category `budgeted, spent, balance, carryover`
- RISK: normal
- SOURCES: (1) github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md; (2) bcrooker/actual-budget-android/CLAUDE.md
- COUNTER: none found
- PRIMARY: Actual Budget official API docs
- STATUS: supported

## Unresolved / partial claims

### U1 — Exact match rate of LLM categorizers
- Risk: high; cannot verify without running on real data
- Recorded as unresolved — should be tested in Wave 4 if user wants hard numbers

### U2 — Best financial insight report pattern (markdown vs HTML vs dashboard)
- Risk: normal; depends on subjective design preference
- Recorded as unresolved — design decision, not research finding

### U3 — Whether GnuCash should be default storage backend for new apps
- Risk: high; architectural decision
- Recorded as unresolved — depends on multi-currency needs and concurrency model
