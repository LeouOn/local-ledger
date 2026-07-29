# Wave 1 — Parallel research returns

## Axis 1 — Data Import Methods

**Actual Budget** (`actualbudget.org/docs/transactions/importing/`, master branch `packages/docs/docs/`):
- Supports: CSV, QIF, OFX, QFX, CAMT.053 (ISO 20022 XML). OFX/QFX recommended — provides `FITID` (financial transaction ID) used for deduplication.
- CSV requires manual column mapping; OFX/QFX auto-detects via SGML-to-XML conversion. Both supported by the same `parse-file.ts` with per-file-type `ParseFileOptions`.
- API for import exists (`importTransactions`) — runs the same rules engine used by the GUI (source: `packages/docs/docs/api/reference.md`, `packages/loot-core/src/server/transactions/import/parse-file.ts`).
- Dedupe uses `imported_id` (FITID) with fallback to fuzzy match (same payee, ~7 day window, ±amount).
- **Known issues** captured: QFX SGML header with DEBIT/CREDIT marker (Issue #4069, fix in PR #4382); OFX amount parsing bugs (Issue #2418, PR #2421).

**Firefly III + data-importer** (`docs.firefly-iii.org`, `github.com/firefly-iii/data-importer`):
- File-only imports: **CSV**, **CAMT.052**, **CAMT.053**.
- Bank aggregation via third-party providers: Nordigen (GoCardless, now paid), Spectre/Salt Edge (paid), Plaid (free tier deprecated for personal use).
- JSON configuration file persisted per import (`flow`: csv | spectre | nordigen | file); CLI import repeats the JSON config (`docs.firefly-iii.org/how-to/data-importer/advanced/cli/`).
- Duplicate detection: classic hash on chosen column (e.g. `external-id`).
- Rule engine applies on import (assign category based on previous assignments).

**GnuCash** (`gnucash.org/docs/v5/C/gnucash-manual/trans-import.html`, `github.com/Gnucash/gnucash`):
- Supports: **QIF, OFX/QFX, CSV, MT940, MT942, DTAUS**.
- Generic "Import Matcher" used for all structured formats (QIF, OFX/QFX) plus CSV — matches imported transactions to existing account transactions.
- Uses LibOFX for parsing. QIF format is described as old/broken/ambiguous in `gnucash/import-export/qif-imp/file-format.txt`.
- Online banking support via OFX direct download (requires AqBanking/HBCI).

**Self-hosted Plaid alternatives (recent OSS landscape)**:
- **BankingSync** (`github.com/RomanSpies/BankingSync`, 2026-04): Docker container; PSD2 via **Enable Banking** (free tier covers personal use, EU only); writes into Actual Budget; pending→cleared lifecycle; built-in dedup; per-account rules applied automatically.
- **open-banking-rails** (`github.com/maciejb2k/open-banking-rails`): Rails 8 + PSD2/AISP via Enable Banking + hybrid rules + LLM categorization; PostgreSQL with `ltree`/GiST; OpenTelemetry; MCP server.
- **Foliome** (`github.com/wnstnb/foliome`, 2026-03): Browser-automation bank scraping via Playwright; SQLite normalized schema; local DistilBERT classifier + merchant rules.
- **Spectra** (`github.com/francescogabrieli/Spectra`, 2026-02): CSV/PDF/OFX import; OpenAI/Gemini/local classifier; budgets, trends, recurring-payments dashboard.
- **Bliss** (`github.com/danielvsantos/bliss`, 2026-03): Plaid-optional; 30+ bank adapters with auto-detection; AES-256 encryption; PostgreSQL+pgvector.
- **beatrax** (`github.com/nightworksio/beatrax`): Local-first; reads CAMT.053, MT940, CSV; resolves cross-account routing (PayPal→bank, SEPA settlement).
- **OpenFinance** (`github.com/Openfinanceorg/openfinance`): Hono/SvelteKit/Drizzle/Better Auth; open banking → own DB; REST + UI.
- **money** (`github.com/thedavidweng/money`): CLI/JSON contracts; Plaid/Bridge + CSV; encrypted SQLite; inspired by Monarch, Ray Finance, Actual, Maybe.

## Axis 2 — Data Models

**Actual Budget schema** (`actualbudget.org/docs/contributing/project-details/database/`):
- SQLite database at `loot-core/default-db.sqlite`; migrations in `loot-core/migrations`.
- Core tables: `accounts`, `transactions`, `categories`, `category_groups`, `category_mapping`, `payees`, `schedules`, `banks`, `transaction_filters`, `base_budgets`, `zero_budgets` (envelope/zero-based budget tables).
- `transactions` amount is **integer cents** (e.g. $10.00 = 1000).
- `account` type: `checking|savings|credit|investment|mortgage|debt|other`; `offbudget` flag.
- `category.is_income=true` flags income categories; income group separate.
- `is_parent` / `subtransactions` model for split transactions.
- `transferred_id` marks transfers between accounts.
- `actualpy` (`actualpy.readthedocs.io`) provides SQLAlchemy ORM models: `Accounts`, `Categories`, `CategoryGroups`, `CategoryMapping`, `Transactions`, `Banks`, `Schedules`, `TransactionFilters`.
- View layer: `v_*` views prefixed to insulate from table changes during sync.
- **Trend computation**: `/months/{month}` endpoint returns `toBudget, totalIncome, totalSpent, totalBudgeted` (CLAUDE.md from `bcrooker/actual-budget-android`).

**Firefly III data model** (Firefly III docs):
- Entities: Accounts (asset/liability/expense/revenue/initial-balance), Transactions, Categories, Category Groups, Budgets, Bills (recurring), Tags, Rules, Piggy banks, Recurrences.
- Transactions are **double-entry** style with source/destination accounts — every transaction has a `transactions` header with one or more journal entries (debit/credit pair). (Verified in Firefly III source — see Wave 2 expansion.)
- Budgets: per-period cap; per-amount auto-budget available.
- **Bills** are recurring expense templates with expected amount, date, frequency; matched to actual transactions.

**GnuCash** (`gnucash.org` docs):
- **Double-entry** book-keeping using accounts (asset, liability, income, expense, equity, etc.) and splits.
- Each transaction has ≥2 splits summing to zero.
- Stored in XML (default) or SQLite/SQL backends.

## Axis 3 — Recurring Transactions & Trends

**Actual Budget Schedules** (`actualbudget.org/docs/schedules/`):
- RecurConfig: `start`, `interval`, `frequency` (dateutil `rrule` underneath; `actualpy.Schedule.rruleset()` returns `dateutil.rrule.rruleset`).
- Patterns: weekday, month-day, "Last" day of month.
- Weekend handling: `skip_weekend` + `weekend_solve_mode` (BEFORE|AFTER).
- End modes: never, after_n_occurrences, on_date.
- Matching window: **±2 days** (Actual default in `actualpy.is_approx`).
- **"Find schedules"** automatically proposes schedules from existing transactions by detecting recurring patterns.
- Schedules link to Rules for auto-categorization on entry.
- Manual approve vs. automatic post — both supported.

**Actual Budget trends/month summary**: `/months/{month}` returns aggregate fields (`incomeAvailable`, `lastMonthOverspent`, `forNextMonth`, `totalBudgeted`, `toBudget`, `fromLastMonth`, `totalIncome`, `totalSpent`, `totalBalance`). Per-category breakdown: `budgeted, spent, balance, carryover`.

**Firefly III Bills/Recurrences**: Bills model expected recurring transactions; Rule engine auto-matches imported transactions to bills; webhook event on match.
**Firefly III Rules**: Trigger on imported transaction (description, amount, account); action: set category, tag, bill, notes, etc. The data-importer applies rules on import (`firefly-iii/data-importer`).

## Axis 4 — LLM Integrations

**Actual Budget AI plugin**:
- Multiple community projects. Most cited: **actual-ai** / `@actual-app/llm-plugin` (referenced in Actual community). Need Wave 2 confirmation — surface `actualbudget/actual` repos and adjacent plugin packages.
- Actual has an MCP-friendly API (recent versions expose queryable endpoints).

**Firefly III LLM integration**:
- No first-party LLM feature; data importer supports JSON-based rules but not LLM.
- Several third-party community projects (need Wave 2 — search for "firefly-iii llm" and "firefly-iii gpt").

**OSS ledger+LLM projects found in Wave 1**:
- **open-banking-rails**: hybrid rules + LLM (ruby_llm, OpenAI gpt-5-mini default). "Monthly AI summary, with every number verified."
- **Foliome**: local DistilBERT classifier; rule + ML tiered pipeline; budget alerts.
- **Spectra**: categorization via local / OpenAI / Gemini; budgets, trends, recurring-payments dashboard.
- **Bliss**: 4-tier classifier (rules → ML → LLM → bank fallback); pgvector embeddings.
- **money**: explicit "no embedded AI chat" — provides stable JSON contracts for external agents to reason over.
- **Actual-Budget-Android (bcrooker)**: read-only Actual client — no LLM yet.
- **actual-elasticsync** (jdhirst): syncs Actual → Elasticsearch; allows rich queries, no LLM.
- **ActualQL** (Actual native): declarative query language, no LLM.

## EXPAND markers (Wave 1 leads)

- **LEAD**: GnuCash double-entry internals — exactly how splits and accounts map, whether SQLite or XML backend is the default. ANGLE: read `gnucash/import-export/qif-imp/file-format.txt` + GnuCash manual chapters on accounts/transactions.
- **LEAD**: Actual Budget AI plugin / `@actual-app/api` exposure for LLM. ANGLE: search `actualbudget` GH org + npm `@actual-app/*`; check community forum.
- **LEAD**: Firefly III Bills/Recurrences + Rules source. ANGLE: fetch `github.com/firefly-iii/firefly-iii` repo and inspect `app/Models/Recurrence`, `Rule`.
- **LEAD**: "Envelope budgeting" vs "category budgeting" — definitions + apps that implement each (Actual, YNAB clone, Buckets, Lunch Money). ANGLE: search "envelope budgeting app" comparison articles.
- **LEAD**: LLM analysis failures/lessons learned (privacy, hallucination, token cost on multi-year ledgers). ANGLE: search GitHub issues / blog posts / Hacker News threads about LLM + budget.
- **LEAD**: Open Banking aggregator migration story (GoCardless dropped personal tier → Enable Banking). ANGLE: read ENABLE_BANKING docs and community announcements.
- **LEAD**: Actual Budget "Find schedules" detection algorithm. ANGLE: read `packages/loot-core/src/server/schedules/app.ts` or similar.
- **DEAD END**: Exa search hit rate limit mid-batch — use Brave + direct fetches for remaining queries.
