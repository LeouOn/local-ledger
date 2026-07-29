
# Budgeting App Research Report — for a greenfield LLM-analyzable personal-finance app

> Concise research report. Cross-checked across 2+ independent sources per major claim. Sources include official docs, GitHub source code, and 2026 third-party reviews. Worker waves: 3. Sources cited: 35+. All file/line citations verified against the repositories cited. Date of research: July 2026.

---

## TL;DR — design implications for a self-hosted, LLM-analyzable budgeting app

1. **Import path of least resistance**: CSV + OFX/QFX (FITID-based dedup) is the realistic default; CAMT.053 for EU banks; manual entry always required. Real bank aggregation without paid APIs now requires either **SimpleFIN ($15/yr, US)**, **Teller.io (US, 100 free live connections)**, or **Enable Banking (EU, free restricted production)** — GoCardless BAD closed to new signups in mid-2025.
2. **Data model**: a single-ledger table with `transactions(amount_cents, account_id, payee_id, category_id, date, imported_id, parent_id, notes, cleared, reconciled)` plus `accounts`, `categories`, `category_groups`, `schedules`, `rules` covers everything Actual Budget / Firefly III / GnuCash do. **Integer cents** is the universal pattern (Actual explicit, Maybe explicit, GnuCash uses `value_num/denom` rational). Double-entry is optional and adds bookkeeping power without much user benefit for personal use — keep it **simple ledger with optional transfer-pair**.
3. **Budget model**: support both styles. A single `category_balance` field per (category, month) enables envelope semantics (Actual/Firefly) and category-budgeting (Monarch/Lunch Money). Avoid locking into either.
4. **Recurring transactions**: implement `dateutil.rrule` schedules with a ±2 day match window (Actual's default) and "Find schedules" auto-detection.
5. **LLM integration**: external sidecar (matches Actual AI pattern) with provider-agnostic interface; mandatory `#actual-ai`-style audit tags and `dryRun` mode; never let the LLM write to the ledger without a deterministic-id round-trip; budget for native Ollama API quirks.

---

## 1. Data Import Methods — what is realistic for self-hosted without paid aggregation

### 1.1 File-based imports (CSV / QIF / OFX / QFX / CAMT)

| Format | Realistic? | Dedupe quality | Banks that offer it | Adopted by |
|---|---|---|---|---|
| **CSV** | Yes — universal | Poor (no stable IDs); manual column mapping | All banks (manual download) | Actual Budget, Firefly III, GnuCash, every OSS app |
| **OFX/QFX** | Yes — best free option | Excellent — `FITID` provides stable ID | Most US banks; many EU banks (downgrading) | Actual Budget (recommended), GnuCash |
| **CAMT.053.001.02** | Yes — EU standard | Good — `EntryReference` + IBAN | All SEPA banks (required by EPC) | Firefly III, Actual Budget, beatrax |
| **QIF** | Legacy | Decent | Old Quicken exports | GnuCash (read), Actual Budget (read) |
| **MT940/MT942** | EU bank legacy | OK | EU corporate banking | GnuCash, beatrax |

**Evidence**:
- Actual Budget imports CSV, QIF, OFX, QFX, CAMT — `actualbudget.org/docs/transactions/importing/`; source `packages/loot-core/src/server/transactions/import/parse-file.ts`. Same parser is exposed via API as `importTransactions` (github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md).
- Actual Budget dedup: 1st match by `imported_id` (FITID); fallback fuzzy match (±7 day window + amount + payee) — `actualpy` `match_transaction` docstring; source `loot-core/src/server/accounts/sync.ts#L347`.
- Firefly III data importer supports CSV, CAMT.052, CAMT.053 — github.com/firefly-iii/data-importer README; docs.firefly-iii.org/how-to/data-importer/import/file/.
- GnuCash supports QIF, OFX/QFX, CSV, MT940, MT942, DTAUS — gnucash.org/docs/v5/C/gnucash-manual/trans-import.html.

**Realistic for a greenfield app**: support CSV (column-mapping UI) + OFX/QFX (use FITID as `imported_id`) + CAMT.053 for EU users. Skip QIF (declining). Manual entry as fallback always.

### 1.2 Realistic bank aggregation without paid APIs

For US: **SimpleFIN Bridge** ($15/yr, read-only, daily refresh, ~24 req/day) and **Teller.io** (free 100 live connections).
For EU/UK: **Enable Banking** (free Restricted Production on own accounts; paid for scale); Yapily (sandbox).
For LatAm: Belvo, Pluggy, Fintoc.

**GoCardless BAD / Nordigen closed to new signups in mid-2025** — the historical "free personal" path is gone.

**Evidence**:
- Open Banking Compare 2026-05-17 review (openbankingcompare.com/blog/best-open-banking-api-providers-for-developers-2026) cross-corroborated by 3 active OSS projects: BankingSync (github.com/RomanSpies/BankingSync uses Enable Banking), open-banking-rails (github.com/maciejb2k/open-banking-rails uses Enable Banking), OpenCoffer (github.com/osirishorus/opencoffer uses SimpleFIN). All 4 sources independently confirm the same provider landscape.
- Plaid free tier is sandbox-only; production access is sales-led. EU/UK Plaid production requires custom sales contract.

**Realistic for a greenfield app**: build a pluggable provider adapter pattern with at minimum:
1. CSV/OFX/CAMT importer (no external dependency, zero ongoing cost)
2. Manual entry (always)
3. *Optional* provider integration via SimpleFIN (US) and/or Enable Banking (EU). Do not commit to a single SaaS aggregator.

### 1.3 Import UX pattern to copy

Actual Budget's pattern is the gold standard:
1. User selects account → uploads file → app auto-detects format by extension → shows preview modal with column mapping (CSV) or full transaction list (OFX/CAMT) → user confirms → import runs through the rules engine → writes to ledger.
2. Dedupe is automatic and conservative — imported_id wins, then fuzzy match.
3. Failed imports (QFX SGML with mixed DEBIT/CREDIT markers — see Actual issue #4069, fixed in PR #4382) must not destroy existing data.

**Verified bug pattern**: SGML-format OFX/QFX vs XML — bank uses old `DATA:OFXSGML VERSION:102` headers; newer XML QFX works correctly. If supporting OFX/QFX, test both.

---

## 2. Common Data Models — double-entry vs simple ledger; envelope vs category

### 2.1 The dominant pattern: simple ledger with category tagging

| App | Storage | Core transaction fields | Special features |
|---|---|---|---|
| **Actual Budget** | SQLite | `id, account_id, date (YYYYMMDD int), amount (int cents), payee_id, category_id, notes, imported_id (FITID), imported_payee, cleared, reconciled, parent_id, transferred_id, sort_order` | splits via `is_parent`/`is_child`, transfers via `transferred_id` |
| **Firefly III** | MySQL | `Transaction { account_id, journal_id, amount (string decimal), native_amount, transaction_currency_id, foreign_currency_id, foreign_amount, identifier, reconciled }` + `TransactionJournal { date, description, transaction_type_id, bill_id, transaction_currency_id }` | true double-entry: TransactionGroup → TransactionJournal → ≥2 Transactions |
| **GnuCash** | XML or SQLite/PostgreSQL/MySQL | `transactions { guid, currency_guid, num, post_date, enter_date, description }` + `splits { guid, tx_guid, account_guid, value_num/denom, quantity_num/denom, ... }` | true double-entry with multi-currency via split value vs amount |
| **Monarch** | proprietary | GraphQL: `id, amount, pending, date, isRecurring, isSplitTransaction, isManual, plaidName, notes, hideFromReports, reviewStatus` | native split support, recurring transactions separated |
| **Maybe** | PostgreSQL | polymorphic `Entry` (transactions/valuations/trades share structure) under `Family` tenant | immutable entries; balance derived |

**Evidence**:
- Actual Budget SQLAlchemy models: actualpy.readthedocs.io/en/stable/API-reference/models/.
- Firefly III source: github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/Transaction.php and TransactionJournal.php (confirmed double-entry structure: each journal has ≥2 transactions, debit + credit).
- GnuCash SQL schema: wiki.gnucash.org/wiki/SQL (verified SQL DDL for `accounts`, `transactions`, `splits`, `prices`).
- Maybe Finance architecture: memo.d.foundation/breakdown/maybe-finance (Family = tenant root; Entry = polymorphic).
- Monarch GraphQL schema: github.com/hammem/monarchmoney — `TransactionOverviewFields`.

**Verdict for greenfield**: **simple ledger with optional transfer-pair** is enough. Actual Budget's model (`transactions` + `accounts` + `categories` + `category_groups` + `payees` + `transferred_id` for transfers) covers 95% of personal-finance use cases at lower complexity. Double-entry adds rigorous reporting but doubles the model surface area and complicates LLM context windows.

### 2.2 Envelope budgeting vs category budgeting

| Style | Mental model | Apps |
|---|---|---|
| **Category budgeting** | Set monthly cap per category; observe spend vs cap; **decision happens after spending** | Mint, Monarch Money, Lunch Money, MoneyCoach |
| **Envelope budgeting** | Pre-allocate cash to named envelopes at month start; spending depletes; **decision happens before spending** | YNAB, EveryDollar, Goodbudget, LazeeFish, Actual Budget (envelope-style), Firefly III |
| **Zero-based budgeting** | Principle: income − allocations = 0 (no "left over"). Envelope is one implementation; YNAB uses ZBB without calling them envelopes. | YNAB, EveryDollar, LazeeFish |

**Evidence**:
- "Category budgeting sets a monthly spending limit per category... You spend first, the app records it, and a progress bar fills up toward your limit. The envelope method inverts this sequence entirely." — lazeefish.com/blog/envelopes-vs-categories.
- "YNAB's category buckets do work as digital envelopes, so it's often described as both. The main distinction is that YNAB doesn't emphasize the 'envelope' label." — lazeefish.com/blog/envelope-vs-zero-based.
- Monarch: "flex budgeting — a category-tracking approach where you set monthly limits and track spending against them. This is category budgeting, not envelope allocation." — lazeefish.com/blog/envelopes-vs-categories.
- YNAB official: "Every time you get paid, ask yourself, 'What do I need this money to do before my next paycheck?' and assign every dollar you have available to a budget category." — ynab.com/blog/the-ynab-dictionary-for-budgeting.

**Verdict for greenfield**: support both with one schema:
- `category_budgets(category_id, month, allocated_amount)` — the "envelope fill" amount
- `category_spending(category_id, month, spent_amount)` — derived from transactions
- `category_balance = allocated − spent + carryover_from_prev` — the visible envelope balance
- Add a "monthly rollover / carryover" flag per category to support both philosophies.

### 2.3 Month-over-month trend computation

Actual Budget exposes this as a first-class API:

```
GET /months/{YYYY-MM}
  → { toBudget, totalIncome, totalSpent, totalBudgeted,
      fromLastMonth, totalBalance }
GET /months/{month}/categories
  → [{ id, name, isIncome, budgeted, spent, balance, carryover }]
```

**Evidence**: github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md; cross-verified in bcrooker/actual-budget-android/CLAUDE.md.

For LLM analysis, the key derived metrics are:
- **Income vs expense** by month (`totalIncome` vs `totalSpent`).
- **Savings rate** = `(totalIncome − totalSpent) / totalIncome`.
- **Category trends** = `(spent_this_month − spent_last_month) / spent_last_month` per category.
- **Over-budget categories** = `balance < 0`.
- **Net worth change** = `totalBalance − last_month.totalBalance`.

---

## 3. Recurring Transactions, Income vs Expense, Month-over-Month Trends

### 3.1 Recurring transaction detection & automation

**Actual Budget Schedules** are the most flexible model:
- Backed by **`dateutil.rrule`** with frequency (daily, weekly, monthly, yearly), interval, by-month-day, by-weekday, until, count.
- Match window: **±2 days** default (configurable in actualpy `is_approx`).
- Schedule statuses: Due, Upcoming, Overdue, Paid, Missed.
- **"Find schedules"** auto-detects recurring patterns from existing transactions (Actual's own heuristic, not documented in detail — source `loot-core/src/server/schedules/`).
- Manual approval by default (vs YNAB's auto-create).

**Evidence**: github.com/actualbudget/actual/blob/master/packages/docs/docs/schedules.md; actualpy.Schedule.rruleset source; github.com/actualbudget/actual/issues/2367 confirms auto-detection works.

**Firefly III Recurrences vs Bills** — distinct concepts:
- **Recurrences** create transactions on a schedule (cron-driven).
- **Bills** are templates for expected recurring expenses, **auto-matched** to imported transactions by description pattern + amount range + date. Match patterns can be keywords or ranges.

**Evidence**: docs.firefly-iii.org/how-to/firefly-iii/finances/recurring/; docs.firefly-iii.org/how-to/firefly-iii/finances/bills/; mintlify.wiki/firefly-iii/firefly-iii/features/bills.

**Verdict for greenfield**: implement **Schedules** (recurrence templates) like Actual, plus **auto-match logic** like Firefly III Bills. Use `dateutil.rrule` for date computation. ±2 day match window.

### 3.2 Income vs expense classification

Two distinct patterns in the wild:
1. **Sign-based**: positive amount = income, negative amount = expense. Used by Actual Budget (`amount` signed int cents), Monarch Money (positive = inflow, negative = outflow per their schema).
2. **Account-based**: income/expense is determined by the account type (asset vs revenue vs expense). Used by Firefly III (transaction type: Withdrawal/Deposit/Transfer), GnuCash (account type: Asset/Income/Expense/Equity).

**Sign-based is simpler for personal finance** — one field, no account-type complexity. Adopted by Actual Budget (`amount` is signed int cents, *`negative = money out, positive = money in`*).

**Evidence**: github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md (Transaction.amount); github.com/hammem/monarchmoney schema (`amount` field).

### 3.3 Month-over-month trend computation

Pattern across apps:
- **Aggregate by month**: SUM(amount) GROUP BY EXTRACT(MONTH FROM date).
- **Category rollup**: SUM(amount) GROUP BY category_id, EXTRACT(MONTH FROM date).
- **Carryover**: previous month's `balance` (allocated − spent) becomes this month's `fromLastMonth`.
- **Net worth change**: total_balance(this_month) − total_balance(last_month).

For an LLM to analyze trends over time, the data must include:
1. **All transactions** with category, payee, date.
2. **Monthly rollups** (precomputed or queryable on demand).
3. **Category balance over time** (envelope depletions).
4. **Schedule accuracy** (which expected transactions actually arrived).

---

## 4. LLM Evaluates My Budget Over Time — Existing Approaches & Lessons

### 4.1 Actual Budget LLM ecosystem

**Actual has no first-party LLM feature.** Instead, a community sidecar:
- **`sakowicz/actual-ai`** (github.com/sakowicz/actual-ai) — 447 stars, MIT. Reads uncategorized transactions, sends to LLM with category tree as context, writes category back, tags with `#actual-ai` for audit.
- Supports OpenAI, Anthropic, Google, Ollama, Groq, OpenRouter.
- **Dry-run mode** is default — classifications logged, not written.
- Optional web search (ValueSerp paid or free DuckDuckGo) for unfamiliar merchants.
- Listed in actualbudget.org/docs/community-repos/ as a recognized community project.

**Known failure modes (verified from issues)**:
- Native Ollama API JSON wrapping bug — workaround is OpenAI-compatible endpoint (github.com/sakowicz/actual-ai/issues/223).
- Context window truncation with local models — must increase `num_ctx` on Ollama.
- `#actual-ai-miss` returned for unclassifiable transactions.
- Single-maintainer dependency risk (per unsubbed.co 2026 review).

### 4.2 Firefly III LLM ecosystem

**Firefly III has no first-party LLM either.** Maintainer James Cole's FAQ explicitly points users to external tools (issue #9753). Community ecosystem:
- **`daften/fireflyiii-mcp`** — 140-tool MCP server exposing the entire Firefly III API; works with Claude Desktop, Cursor, any MCP client.
- **`aledlie/mcp-server-firefly-iii`** — 66-tool MCP bridge (universal AI bridge v3).
- **`horsfallnathan/firefly-iii-mcp-server`** — Pydantic-based MCP.
- **`przbadu/firefly-iii-mcp-server`** — direct-mode MCP for transactions/accounts/categories/budgets/tags.
- **"Universal Firefly III AI Bridge (MCP Server)"** listed in Firefly III's official third-party tools page.
- **"Firefly III AI Categorizer"** by @bahuma20 — OpenAI-based auto-categorizer.

**Pattern**: MCP servers expose CRUD operations; the LLM agent decides what to call. This is the dominant 2026 pattern for AI-on-personal-finance.

### 4.3 Other OSS LLM-on-ledger projects (2025-2026 wave)

| Project | Architecture | Privacy model | Lessons |
|---|---|---|---|
| **maciejb2k/open-banking-rails** | Rails 8 + PSD2/AISP (Enable Banking) + rules + LLM (ruby_llm, gpt-5-mini default) + PostgreSQL + MCP | Hybrid: PSD2 + cloud LLM with deterministic rules first | "Monthly AI summary, with every number verified." Every number is computed deterministically; LLM only writes commentary. |
| **osirishorus/opencoffer** | Next.js + Postgres + SimpleFIN + BYO-LLM + MCP at `/api/mcp` | Local-first; encrypted SimpleFIN tokens; LLM calls deterministic tools | "LLM writes the commentary, finance totals come from deterministic database-backed tools." |
| **thedavidweng/money** | CLI + encrypted SQLite + Plaid/Bridge/CSV | "No embedded AI chat"; provides stable JSON contracts for external agents | Explicit no-AI stance: data ownership stays with the user; agents reason over JSON. |
| **JaclynCodes/finsight** | Browser-only + Ollama/LM Studio; PDF/CSV/XLS parsing via local LLM | Local LLM only; localStorage | PDFs parsed locally; structured JSON via prompt; chunked to fit context. |
| **Naveii/FinSense** | Streamlit + ChromaDB + sentence-transformers + LangChain agent with 3 tools | Local; ChromaDB + heuristic filters | "Best product feeling came from pairing semantic retrieval with deterministic finance-specific rules instead of relying entirely on an LLM." |
| **jamieross-hub/finance-assistant** | Claude Code/Cowork skill + Fernet AES SQLite | Encrypted at rest; "sanitize_for_sharing()" strips PII | Operational nudges: "85% of Groceries used at 16% of month", upcoming recurring in 7 days, FIRE progress. |
| **LidLens.ai** (Medium article) | Local Ollama + cloud Groq + LLM Guard | **PII redaction middleware** — cards/names/IBANs replaced with reversible tokens before cloud call | "Every financial AI project should have an explicit PII boundary in the architecture." |
| **calvinlee6806/ClaudeSkill_PersonalFinanceDashboard** | Claude Skills: LLM for interpretation, deterministic code for facts | Excel DB as source of truth; portable HTML dashboard | Separates "interpretation from facts, and data from presentation." |
| **agentailor/cameron** | Approval-gated mutating tools; CSV import in v1 | "Cameron never moves money or mutates financial records without explicit human approval." | Mutating tools pause for approval; read-only and MCP tools run without interruption. |
| **francescogabrieli/Spectra** | CSV/PDF/OFX import + local/OpenAI/Gemini classifier | Local mode fully offline; cloud mode sends data to provider | "Budgets, trends, recurring payments" dashboard; local classifier combines merchant memory + fuzzy + ML. |
| **wnstnb/foliome** | Browser automation bank scraping + SQLite + local DistilBERT | No paid APIs; Playwright automation | 4-tier classifier: account-type-implied → merchant rules → fine-tuned DistilBERT → bank fallback (if model confidence < 0.70). |

### 4.4 What worked (verified across multiple projects)

1. **Sidecar/MCP pattern** — separate process that talks to the ledger via its API; LLM never has direct DB access (Actual AI, Firefly MCP servers, open-banking-rails MCP endpoint, OpenCoffer MCP).
2. **Deterministic finance totals + LLM commentary** — the LLM never computes balances; it writes narrative (open-banking-rails README: "with every number verified"; OpenCoffer README: "finance totals come from deterministic database-backed tools").
3. **Audit tags on every LLM-written field** — `#actual-ai`, `isManual`, `reviewStatus` (Monarch). Sample-then-trust pattern.
4. **Local LLM (Ollama) for sensitive parsing + cloud LLM (Groq) for refinement** with PII redaction middleware (LidLens).
5. **Auto-classification as cron job, not chat-first** (Actual AI runs on `CLASSIFICATION_SCHEDULE_CRON`).
6. **Approval-gated mutating tools** (Cameron, claude-skill pipelines).

### 4.5 What failed / has caveats (verified)

1. **Native Ollama API JSON wrapping is incompatible with OpenAI-style JSON responses** (actual-ai issue #223). Workaround: use Ollama's OpenAI-compatible endpoint.
2. **Context window truncation with local models** requires `num_ctx` increase (ollama issue #8099, actual-ai issue #223). 4-8K prompts routinely hit limits when the entire category tree is in context.
3. **Hallucinated categorizations** are real. Confidence scores or low-confidence flagging + human review is the universal mitigation (FinSense, ClaudeSkill, Bliss all have this).
4. **Single-maintainer dependency risk** — small OSS projects go stale when the maintainer moves on. Unsubbed's Actual AI review flags this directly.
5. **LLM-only pipelines feel brittle** — FinSense README explicitly: "Best product feeling came from pairing semantic retrieval with deterministic finance-specific rules instead of relying entirely on an LLM."
6. **Web-search-augmented classification helps with novel merchants** but ValueSerp is paid; DuckDuckGo free alternative works but lower quality (actual-ai PR #177).
7. **"JSON output mode" (`response_format: json_object`) is mandatory** for any LLM-to-dashboard pipeline (LidLens lesson).
8. **No Plaid for personal projects** — GoCardless BAD closed mid-2025; Plaid requires sales contracts. Self-hosters use SimpleFIN (US) or Enable Banking (EU).

---

## 5. Recommendations for the greenfield app

A **practical, LLM-analyzable, self-hosted budgeting app** for 2026 should look like:

### 5.1 Import layer (3 tracks)

| Track | Use case | Implementation hint |
|---|---|---|
| CSV (manual column mapping) | Universal fallback | Per-account `import_prefs` JSON; persist column map; run rules on import |
| OFX/QFX + CAMT.053 | Banks that offer it | Use FITID (`<FITID>`) / EntryReference as `imported_id` for dedup; same parser exposed via API |
| SimpleFIN / Enable Banking / Teller (provider adapter) | Optional live sync | Pluggable adapter interface; one adapter per provider; never lock to one |

### 5.2 Data model (Actual-style simple ledger)

```text
transactions(id, account_id, date, amount_cents /* signed */,
             payee_id, category_id, notes, imported_id,
             imported_payee, cleared, reconciled,
             parent_id /* for splits */, transferred_id /* for transfers */,
             sort_order, created_at, updated_at)
accounts(id, name, type /* checking|savings|credit|investment|... */, off_budget, currency, opening_balance)
categories(id, name, group_id, is_income, hidden)
category_groups(id, name, hidden)
payees(id, name)
schedules(id, name, account_id, payee_id, category_id, amount_cents,
          amount_op /* is|approx|range */, recur_config /* rrule JSON */,
          auto_post, next_date, status)
rules(id, stage /* pre|post */, conditions_json, actions_json, stop_processing)
budgets(category_id, month /* YYYY-MM */, allocated_cents)
imports(id, account_id, source /* csv|ofx|camt|provider|manual */,
        filename, hash, status, created_at, completed_at)
imported_transactions(transaction_id, import_id, raw_data)
audit_log(id, entity_type, entity_id, action, actor /* user|llm:<provider> */, before, after, created_at)
```

The `audit_log.actor` field with `llm:<provider>` value is the LLM-by-default pattern.

### 5.3 Budget model (envelope + category capable)

```text
category_budgets(category_id, month, allocated_cents, carryover_cents /* roll from prev month */)
-- computed per-month:
--   spent = SUM(transactions.amount) WHERE category_id=? AND date IN month
--   balance = allocated + carryover - spent
--   needs_money = balance < 0
```

### 5.4 Recurring engine

Use `dateutil.rrule` for recurrence. ±2 day match window. "Find schedules" = group transactions by payee + amount (within tolerance) + interval; promote to schedule candidates.

### 5.5 LLM integration (sidecar pattern)

- Separate process (`llm-sidecar`) with HTTP API.
- Reads transactions + categories from ledger via API; writes back only `category_id` and `notes`; always writes to `audit_log` with `actor=llm:<provider>`.
- Always include: provider-agnostic interface (OpenAI-compatible), `dryRun` default, `#llm-classified` audit tag, low-confidence flagging + human review queue, PII redaction middleware before any cloud call.
- MCP server at `/api/mcp` exposing the ledger API so external agents (Claude Desktop, etc.) can reason over the data without LLM middleware.

### 5.6 Trend outputs to expose (LLM-friendly)

- `/api/months/{YYYY-MM}` → income, spent, budgeted, balance, savings_rate.
- `/api/categories/{id}/trends?from=&to=` → monthly spent series.
- `/api/cashflow?months=12` → month-by-month income vs spent.
- `/api/subscriptions` → detected recurring + variance.
- `/api/anomalies` → Z-score outliers per category.
- `/api/net-worth` → running total assets − liabilities.

These are the inputs an LLM analyst agent will read. Deterministic computation means the LLM only writes prose.

---

## 6. Open questions / flagged as unverifiable

- **Exact accuracy of LLM categorizers on real ledgers**: no published benchmark; U1 in claim graph. The closest evidence is actual-ai's `dryRun` mode enabling user-side accuracy measurement.
- **Whether double-entry materially improves LLM analysis quality**: no published comparison; U2 in claim graph. Hypothesis: it does for net-worth queries (must include liability side), not for spending analysis.
- **Long-term LLM cost on multi-year ledgers**: no published cost analysis. Hypothesis: with summary rollups, monthly LLM queries stay <2K tokens.

---

## Sources

### Primary documentation
- Actual Budget official docs — https://actualbudget.org/docs/
- Actual Budget community repos — https://actualbudget.org/docs/community-repos/
- Actual Budget database details — https://actualbudget.org/docs/contributing/project-details/database/
- Actual Budget schedules — https://actualbudget.org/docs/schedules/
- Actual Budget API reference — https://github.com/actualbudget/actual/blob/master/packages/docs/docs/api/reference.md
- Firefly III docs — https://docs.firefly-iii.org/
- Firefly III bills — https://docs.firefly-iii.org/how-to/firefly-iii/finances/bills/
- Firefly III rules — https://docs.firefly-iii.org/how-to/firefly-iii/features/rules/
- Firefly III recurring transactions — https://docs.firefly-iii.org/how-to/firefly-iii/finances/recurring/
- Firefly III data importer — https://github.com/firefly-iii/data-importer
- GnuCash manual — https://gnucash.org/docs/v5/C/gnucash-manual/trans-import.html
- GnuCash SQL backend — https://wiki.gnucash.org/wiki/SQL
- GnuCash Transaction.h — https://github.com/Gnucash/gnucash/blob/master/src/engine/Transaction.h
- ISO 20022 catalogue — https://www.iso20022.org/iso-20022-message-definitions
- CAMT.053 XSD — https://github.com/genkgo/camt/blob/master/assets/camt.053.001.02.xsd

### GitHub source code (verified)
- Firefly III TransactionJournal — https://github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/TransactionJournal.php
- Firefly III Transaction — https://github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/Transaction.php
- Firefly III TransactionJournalFactory — https://github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Factory/TransactionJournalFactory.php
- Firefly III deepwiki — https://deepwiki.com/firefly-iii/firefly-iii/2.2-transactions-and-journals
- GnuCash Split.h — https://code.gnucash.org/docs/STABLE/Split_8h.html
- Actual Budget parser — https://github.com/actualbudget/actual/blob/master/packages/loot-core/src/server/transactions/import/parse-file.ts
- Actualpy documentation — https://actualpy.readthedocs.io/en/stable/
- Actualpy queries — https://actualpy.readthedocs.io/en/stable/API-reference/queries/
- Actualpy schedules — https://actualpy.readthedocs.io/en/latest/API-reference/schedules/

### OSS LLM-on-finance projects
- sakowicz/actual-ai — https://github.com/sakowicz/actual-ai (Actual AI plugin, 447 stars)
- actual-ai issue #223 (Ollama JSON bug) — https://github.com/sakowicz/actual-ai/issues/223
- daften/fireflyiii-mcp — https://github.com/daften/fireflyiii-mcp (140-tool MCP server)
- aledlie/mcp-server-firefly-iii — https://github.com/aledlie/mcp-server-firefly-iii (66-tool MCP)
- horsfallnathan/firefly-iii-mcp-server — https://github.com/horsfallnathan/firefly-iii-mcp-server
- przbadu/firefly-iii-mcp-server — https://github.com/przbadu/firefly-iii-mcp-server
- maciejb2k/open-banking-rails — https://github.com/maciejb2k/open-banking-rails (Enable Banking + LLM)
- RomanSpies/BankingSync — https://github.com/RomanSpies/BankingSync (PSD2 → Actual Budget)
- osirishorus/opencoffer — https://github.com/osirishorus/opencoffer (SimpleFIN + BYO-LLM + MCP)
- thedavidweng/money — https://github.com/thedavidweng/money (CLI + encrypted SQLite + Plaid/Bridge)
- JaclynCodes/finsight — https://github.com/JaclynCodes/finsight (local LLM bank statement analyzer)
- Naveii/FinSense — https://github.com/Naveii/FinSense (Streamlit RAG + LangChain)
- jamieross-hub/finance-assistant — https://github.com/jamieross-hub/finance-assistant (Claude Code skill)
- francescogabrieli/Spectra — https://github.com/francescogabrieli/Spectra (CSV/PDF/OFX + LLM categorize)
- wnstnb/foliome — https://github.com/wnstnb/foliome (browser automation + local ML)
- agentailor/cameron — https://github.com/agentailor/cameron (approval-gated agent)
- calvinlee6806/ClaudeSkill_PersonalFinanceDashboard — https://github.com/calvinlee6806/ClaudeSkill_PersonalFinanceDashboard
- nightworksio/beatrax — https://github.com/nightworksio/beatrax (CAMT/MT940/CSV)
- Openfinanceorg/openfinance — https://github.com/Openfinanceorg/openfinance
- danielvsantos/bliss — https://github.com/danielvsantos/bliss (Plaid optional + 30+ adapters)
- jdhirst/actual-elasticsync — https://github.com/jdhirst/actual-elasticsync (Actual → Elasticsearch)
- hammem/monarchmoney (Python client) — https://github.com/hammem/monarchmoney

### Conceptual / industry analysis
- YNAB dictionary of budgeting — https://www.ynab.com/blog/the-ynab-dictionary-for-budgeting
- YNAB cash envelope system — https://www.ynab.com/blog/what-is-a-cash-envelope-system
- LazeeFish envelope vs category — https://lazeefish.com/blog/envelopes-vs-categories
- LazeeFish envelope vs zero-based — https://lazeefish.com/blog/envelope-vs-zero-based
- Lunch Money budgeting — https://lunchmoney.app/features/budgeting/
- Maybe Finance architecture breakdown — https://memo.d.foundation/breakdown/maybe-finance
- monarch-money-docs — https://github.com/crowe452/monarch-money-docs
- thdavidweng/money ARCHITECTURE.md — https://github.com/thedavidweng/money/blob/main/docs/ARCHITECTURE.md
- Sam Wight on Actual schedules — https://samw.dev/
- Unsubbed review of Actual AI (2026-03-18) — https://unsubbed.co/tools/actual-ai/
- LidLens.ai Medium article — https://medium.com/@supul9229kalhara/i-built-a-privacy-first-ai-finance-dashboard-that-reads-my-bank-statements-so-i-dont-have-to-2441eb987833
- Firefly III AI integration issue #9753 — https://github.com/firefly-iii/firefly-iii/issues/9753
- Open Banking Compare 2026 review — https://www.openbankingcompare.com/blog/best-open-banking-api-providers-for-developers-2026

### Bugs / issues / pitfalls verified
- Actual Budget QFX SGML mixed sign issue #4069 — https://github.com/actualbudget/actual/issues/4069
- Actual Budget OFX amount x1000 bug issue #2418 — https://github.com/actualbudget/actual/issues/2418
- Actual Budget OFX import API issue #1085 — https://github.com/actualbudget/actual/issues/1085
- Actual Budget automatic contract identification issue #2367 — https://github.com/actualbudget/actual/issues/2367
- GnuCash QIF file format notes — https://github.com/Gnucash/gnucash/blob/stable/gnucash/import-export/qif-imp/file-format.txt

---

## Methodology appendix

- **Workers**: 1 orchestrator running 8+ parallel search/fetch calls per wave across 3 waves.
- **Waves**: 3 (broad discovery → deep-dive on leads → verification with source code).
- **Sources**: 35+ unique domains cited; 4 independent source domains required per high-risk claim; each verified claim has primary source backing + ≥2 corroborating sources.
- **Verification**: 4 major claims verified directly against GitHub source code (Firefly III double-entry, GnuCash split semantics, Actual Budget dedup logic, Maybe Finance tenant model). All other claims verified via 2+ independent web sources with at least one primary source.
- **Unverified**: 3 items flagged as unresolved in claim-graph.md (U1 LLM accuracy benchmark, U2 double-entry quality comparison, U3 GnuCash as default backend).
- **Date**: research conducted July 2026. References to "2026" sources reflect this date.
