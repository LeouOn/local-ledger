# Wave 2 — Parallel research returns

## Axis 1 — Import Methods (deep dive)

**Actual Budget supported formats** (cross-checked):
- CSV (manual column mapping), QIF, OFX, QFX, CAMT.053 (ISO 20022 XML). [Source 1: actualbudget.org/docs/transactions/importing/; Source 2: github.com/actualbudget/actual/blob/master/packages/loot-core/src/server/transactions/import/parse-file.ts]
- Dedupe via `imported_id` (FITID) then fuzzy (±7 day window + amount + payee). [Source: actualpy `match_transaction` doc; Actual source `loot-core/src/server/accounts/sync.ts`]
- API: `importTransactions()` runs the same rules engine as the GUI. [Source: github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md]

**Firefly III supported formats** (cross-checked):
- File-only: CSV, CAMT.052, CAMT.053. [Source 1: github.com/firefly-iii/data-importer README; Source 2: docs.firefly-iii.org/how-to/data-importer/import/file/]
- Bank aggregation: Nordigen/GoCardless (paid), Spectre/Salt Edge (paid), Plaid (free tier restricted). [Source: docs.firefly-iii.org/references/data-importer/third-party-tools/]
- Firefly III author James Cole states "GoCardless used to offer a similar free tier but dropped it for personal projects" — see BankingSync README. [Source: github.com/RomanSpies/BankingSync]
- **Auto-match**: Bills (recurring expense templates) auto-match to imported transactions by description pattern + amount range + date. [Source: docs.firefly-iii.org/how-to/firefly-iii/finances/bills/; mintlify.wiki/firefly-iii/firefly-iii/features/bills]

**GnuCash SQL backend schema** (cross-checked):
- Since 2.4, GnuCash supports SQL via DBI backend (SQLite3, MySQL, PostgreSQL). [Source: wiki.gnucash.org/wiki/SQL]
- `accounts` table: `guid, name, account_type, commodity_guid, commodity_scu, non_std_scu, parent_guid, code, description, hidden, placeholder`.
- `splits` table: `guid, tx_guid, account_guid, memo, action, reconcile_state, reconcile_date, value_num, value_denom, quantity_num, quantity_denom, lot_guid`.
- **Every transaction has at least two splits; credits + debits must balance.** [Source 1: wiki.gnucash.org/wiki/SQL; Source 2: gnucash.org/docs/v5/C/gnucash-guide/basics-entry1.html]
- Note: SQLite3 doesn't support date/timestamp — those fields are CHAR(8) and CHAR(14) respectively.

**CAMT.053.001.02 (ISO 20022 Bank-to-Customer Statement)** (cross-checked):
- Document → BkToCstmrStmt → GrpHdr + Stmt (1..n per account) → Bal (1..n) + Ntry (0..n) → EntryDetails → TransactionDetails. [Source 1: github.com/genkgo/camt/blob/master/assets/camt.053.001.02.xsd; Source 2: iso20022.org/iso-20022-message-definitions]
- Used by EU banks; replaces older MT940 SWIFT format. [Source: ING CAMT.053 PDF; Nordea MIG]

## Axis 2 — Data Models (deep dive)

**Actual Budget entity core (cross-checked)**:
- SQLite at `loot-core/default-db.sqlite`; migrations in `loot-core/migrations`. [Source: actualbudget.org/docs/contributing/project-details/database/]
- Views: `v_*` prefix, regenerated each app start to insulate from table changes during sync.
- `accounts` typed: `checking|savings|credit|investment|mortgage|debt|other`; `offbudget` flag separates budget-tracked vs tracked-only assets. [Source: github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md]
- `transactions.amount` = integer cents (no FP). [Source: actualpy docs; github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md]
- `transactions.is_parent`/`is_child` + `parent_id`/`subtransactions` for splits.
- `transactions.transferred_id` for inter-account transfers.
- `category.is_income` and dedicated "Income" `category_group`.

**Firefly III entity core (cross-checked)**:
- Account types: asset, liability, expense, revenue, initial-balance (source). [Source: docs.firefly-iii.org — Recurrences page references account types]
- Transactions are journal-based (header + 1..n journal entries with debit/credit pairs). [Source: github.com/firefly-iii/firefly-iii source — needs Wave 3 confirmation but referenced in docs]
- Recurring transactions: built-in scheduler with frequency (daily/weekly/monthly/nth-weekday/yearly), weekend skip (skip / Friday-before / Monday-after / keep), end modes (never / after_n / on_date). [Source 1: docs.firefly-iii.org/how-to/firefly-iii/finances/recurring/; Source 2: mintlify.wiki/firefly-iii/firefly-iii/features/bills]
- **Bills** are templates for expected recurring expenses, matched to actual transactions via rule + match pattern. Bills vs Recurrences: Bills verify, Recurrences create. [Source: mintlify.wiki/firefly-iii/firefly-iii/features/bills]

**GnuCash entity core (cross-checked)**:
- Every account has type; types include Asset, Liability, Income, Expense, Equity, Bank, Cash, Credit Card, etc.
- Multi-level chart of accounts (parent_guid).
- Multiple-currency supported; splits carry `value_num/denom` (account currency) AND `quantity_num/denom` (transaction currency).
- Reports: P&L, Balance Sheet, Cash Flow, General Journal — all generated from double-entry invariant. [Source: gnucash.org/features.phtml]

**Envelope vs Category budgeting** (cross-checked):
- **Category budgeting** (Mint, Monarch, Lunch Money's classic mode): set monthly cap per category, observe spend vs cap. Decision happens after spending. [Source 1: lazeefish.com/blog/envelopes-vs-categories; Source 2: lunchmoney.app/features/budgeting/]
- **Envelope budgeting** (YNAB, EveryDollar, Goodbudget, LazeeFish): pre-allocate cash to named "envelopes"; spending depletes the envelope; visual scarcity. [Source 1: lazeefish.com/blog/envelope-vs-zero-based; Source 2: ynab.com/blog/the-ynab-dictionary-for-budgeting]
- **Zero-based budgeting**: principle (income - allocations = 0); envelope is one implementation. [Source: lazeefish.com/blog/envelope-vs-zero-based]
- **Actual Budget**: zero-based / envelope hybrid (per LazeeFish and Actual docs); per-category balance tracked, month-rollover carryover, "to budget" shows unallocated income.

## Axis 3 — Recurring Transactions & Trends (deep dive)

**Actual Budget Schedules (RecurConfig)** (cross-checked):
- Backed by `dateutil.rruleset` (frequency, interval, bymonthday, byweekday, until, count). [Source 1: actualpy Schedule.rruleset; Source 2: actualbudget.org/docs/schedules/]
- Match window: **±2 days** (Actual default in `actualpy.is_approx`). [Source: actualpy docs]
- Schedule statuses: Due, Upcoming, Overdue, Paid, Missed. [Source: samw.dev blog; actualbudget.org/docs/tour/schedules/]
- Amount can be exact / approximate / range (helps match varying utility bills). [Source: samw.dev; github.com/actualbudget/actual/blob/master/packages/docs/docs/schedules.md]
- "Find schedules" auto-discovers recurring patterns from existing transactions. [Source 1: actualbudget.org/docs/schedules/; Source 2: github.com/actualbudget/actual/issues/2367 — request for "automatic contract identification" references this as existing functionality]
- Manual approve vs auto-post: by default schedule entries are NOT auto-created (must be explicitly posted); YNAB's default is the opposite. [Source: samw.dev]

**Firefly III Recurrences vs Bills** (cross-checked):
- Recurrences create transactions on schedule. Bills auto-match imported transactions. Used together: a Recurrence can include a bill to verify payment. [Source: docs.firefly-iii.org/how-to/firefly-iii/finances/recurring/; mintlify.wiki/firefly-iii/firefly-iii/features/bills]
- Rules engine: trigger (description / amount / account / type) + action (set budget / category / tag / notes / bill). Strict vs non-strict modes; "stop processing" per trigger and per action. [Source: docs.firefly-iii.org/how-to/firefly-iii/features/rules/; docs.firefly-iii.org/references/firefly-iii/rule-triggers/]

**Trend computation**:
- Actual Budget: `/months/{YYYY-MM}` API returns aggregate `{toBudget, totalIncome, totalSpent, totalBudgeted, fromLastMonth, totalBalance}`. `/months/{month}/categories` returns per-category `{budgeted, spent, balance, carryover}`. [Source 1: github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md; Source 2: bcrooker/actual-budget-android/CLAUDE.md]
- Firefly III: dashboard reports / charts (cash flow, category breakdown, budget vs spent). Trend computation implemented server-side. [Source: docs.firefly-iii.org — needs explicit citation but verified via reference docs]

## Axis 4 — LLM Integrations (deep dive)

**Actual Budget AI plugin**:
- `sakowicz/actual-ai` (447 stars, MIT) — categorizes uncategorized transactions via LLM. [Source: github.com/sakowicz/actual-ai]
- Providers: openai, openrouter, anthropic, google-generative-ai, ollama, groq. [Source: README + docker hub sakowicz/actual-ai]
- Each classification gets `#actual-ai` tag for audit; dry-run mode default. [Source: README + docker hub]
- Known failure modes:
  - Native Ollama API has JSON wrapping bug (issue #223); workaround is to use Ollama's OpenAI-compatible endpoint. [Source: github.com/sakowicz/actual-ai/issues/223]
  - "actual-ai-miss" returned for transactions the LLM can't classify. [Source: same issue]
  - Web search via ValueSerp (paid) or DuckDuckGo (free, self-hosted). [Source: PR #177 + README]
- Critique: "Single-maintainer community project at 427 stars. No web UI, no classification dashboard, no automatic retry on errors." [Source: unsubbed.co/tools/actual-ai/ — 2026-03-18 review]
- Listing in Actual Budget's own community-repos page. [Source: actualbudget.org/docs/community-repos/]

**Firefly III LLM ecosystem** (cross-checked):
- Firefly III maintainer James Cole's FAQ: "Will you add AI to Firefly III?" — answers with the *external* tooling approach (use ChatGPT Projects with API docs in context). [Source: github.com/firefly-iii/firefly-iii/issues/9753]
- Community-built:
  - `daften/fireflyiii-mcp` — MCP server exposing ~140 Firefly III API operations; stdio and HTTP transports; OAuth and PAT auth. [Source: github.com/daften/fireflyiii-mcp]
  - `aledlie/mcp-server-firefly-iii` — 66-tool MCP server with AI-agnostic bridge. [Source: github.com/aledlie/mcp-server-firefly-iii]
  - `horsfallnathan/firefly-iii-mcp-server` — MCP via Pydantic. [Source: github.com/horsfallnathan/firefly-iii-mcp-server]
  - `przbadu/firefly-iii-mcp-server` — direct-mode MCP for transactions/accounts/categories/budgets/tags. [Source: github.com/przbadu/firefly-iii-mcp-server]
  - Firefly III AI Categorizer by @bahuma20 — OpenAI-based auto-categorizer. [Source: docs.firefly-iii.org/references/firefly-iii/third-parties/apps/]
  - "Universal Firefly III AI Bridge" listed in third-party docs. [Source: docs.firefly-iii.org/references/data-importer/third-party-tools/]
- First-party: NO LLM feature. Maintainer explicitly steers users to external tools (issue #9753 comment).

**Other OSS ledger+LLM projects (Wave 2 additions)**:
- `JaclynCodes/finsight` — Local LLM (Ollama or LM Studio) for PDF/CSV/XLS parsing; browser-only, ChromaDB-free; chat with your data. [Source: github.com/JaclynCodes/finsight]
- `Naveii/FinSense` — Streamlit RAG over CSV transactions; ChromaDB + sentence-transformers; finance-scope guardrail; LangChain agent with 3 tools; deterministic finance filters. [Source: github.com/Naveii/FinSense]
- `jamieross-hub/finance-assistant` — Claude Code/Cowork copilot; CSV/MT940/OFX import; budgets, savings goals, investments, debt, taxes; Fernet AES encryption; Sanitize-for-sharing strips PII. [Source: github.com/jamieross-hub/finance-assistant]
- `Supulkalhara / LidLens.ai` (Medium article) — Local Ollama + cloud Groq hybrid; LLM Guard PII redaction before cloud calls; ChromaDB embeddings for category learning. [Source: medium.com/@supul9229kalhara]
- `calvinlee6806/ClaudeSkill_PersonalFinanceDashboard` — Claude Skills for Import/Update; LLM for cleaning + categorizing, deterministic code for facts; FinanceDB.xlsx as source of truth; portable HTML dashboard. [Source: github.com/calvinlee6806/ClaudeSkill_PersonalFinanceDashboard]
- `agentailor/cameron` — personal finance agent with approval-gated mutating tools; CSV import in v1; v2 = budgets as memory; v3 = external bank MCP server. [Source: github.com/agentailor/cameron]
- `OpenFinance` (Hono/SvelteKit) — Open Banking → own DB; REST + UI. [Source: github.com/Openfinanceorg/openfinance]

**Privacy pattern across OSS LLM-finance projects**:
- Local LLM (Ollama, LM Studio, llama.cpp) → for sensitive parsing; Cloud LLM (Groq, OpenAI) → only after PII redaction with tools like LLM Guard. [Source: LidLens Medium article — explicit pattern]
- Sandboxed contexts, encrypted SQLite at rest, dry-run modes, `#actual-ai` audit tags. [Source: multiple projects]

**LLM-finance failure modes / lessons** (cross-checked):
1. JSON output parsing failures with small/local models → use `response_format: json_object`. [Source: LidLens Medium article]
2. Native Ollama API incompatibility with OpenAI-style JSON response → use OpenAI-compatible endpoint. [Source: actual-ai issue #223]
3. Context window truncation warnings with local models. [Source: actual-ai issue #223, ollama/ollama#8099]
4. Web-search-augmented classification reduces novel-merchant errors. [Source: actual-ai README/PR #177]
5. Always have human review of low-confidence rows before commit. [Source: ClaudeSkill_PersonalFinanceDashboard; Unsubbed Actual AI review]
6. Vendor drift — single-maintainer dependency risk. [Source: Unsubbed Actual AI review]
7. Bank sync via Plaid for personal projects is restricted now → fallback to Plaid via paid tiers, or Enable Banking (EU PSD2 AISP) for free personal use. [Source: BankingSync README; open-banking-rails README]
8. The "best product feeling" from semantic-retrieval + deterministic-finance-rules, not LLM-only. [Source: FinSense README]

## EXPAND markers (Wave 2 leads)

- **LEAD**: Verify "Firefly III transactions are journal-based (header + journal entries)" by reading Firefly III source. ANGLE: clone firefly-iii/firefly-iii, look at `app/Models/TransactionJournal.php` + `Transaction.php`.
- **LEAD**: GnuCash transaction currency handling — `value_num/denom` vs `quantity_num/denom` semantic split. ANGLE: read `gnucash-docs-rst.readthedocs.io/en/latest/guide/C/ch_txns.html`.
- **LEAD**: Actual Budget `importTransactions` rules engine order (when rules fire, dry-run for test). ANGLE: read `loot-core/src/server/accounts/sync.ts`.
- **LEAD**: Ofx2json parser specifics (how FITID is mapped to imported_id). ANGLE: read `packages/loot-core/src/server/accounts/ofx2json.ts`.
- **LEAD**: PII redaction architectures (LLM Guard, Microsoft Presidio, custom regex). ANGLE: read LidLens docs + LLM Guard GitHub.
- **LEAD**: Lunch Money, Monarch, Maybe Finance data models — for comparison with Actual/Firefly. ANGLE: read their API docs.
- **DEAD END**: Exa rate limits continue — fully switching to Brave Search for remaining queries.
