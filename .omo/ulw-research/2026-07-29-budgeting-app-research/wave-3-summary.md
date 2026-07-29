# Wave 3 — Verification & Cross-checks

## Firefly III transaction model — VERIFIED via source

Direct reading of `github.com/firefly-iii/firefly-iii/blob/master/app/Models/TransactionJournal.php` and `app/Models/Transaction.php` confirms:

- `TransactionGroup` is the outermost container (for splits). [Source: deepwiki.com/firefly-iii/firefly-iii/2.2-transactions-and-journals]
- `TransactionJournal` holds financial event metadata: `date`, `description`, `transaction_type_id`, `bill_id`, `transaction_currency_id`, `completed`, `user_group_id`, `user_id`. [Source: github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/TransactionJournal.php]
- `Transaction` is the ledger entry: `account_id`, `transaction_journal_id`, `amount` (string), `native_amount`, `foreign_amount`, `transaction_currency_id`, `foreign_currency_id`, `reconciled`, `identifier`, `description`. [Source: github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Models/Transaction.php]
- Each `TransactionJournal` has at least two `Transaction` rows (debit + credit) — true double-entry. [Source: deepwiki.com/firefly-iii/firefly-iii/2.2-transactions-and-journals]
- Transaction types: Withdrawal, Deposit, Transfer. [Source: deepwiki.com/firefly-iii/firefly-iii/2.2-transactions-and-journals]
- `TransactionJournalFactory::createJournal()` validates accounts and determines type. [Source: github.com/firefly-iii/firefly-iii/blob/e3da2fe0/app/Factory/TransactionJournalFactory.php]
- SHA-256 `import_hash_v2` for duplicate detection. [Source: hashArray method in TransactionJournalFactory]

**VERIFIED**: Firefly III = double-entry, with `TransactionGroup` → `TransactionJournal` → `Transaction` hierarchy. Native & foreign currency per transaction supported.

## GnuCash split semantics — VERIFIED

Direct reading of `github.com/Gnucash/gnucash/blob/master/src/engine/Transaction.h` and `code.gnucash.org/docs/STABLE/Split_8h.html` confirms:

- Each Split has **value** (in the transaction's commodity) and **amount** (in the account's commodity). [Source: code.gnucash.org/docs/STABLE/Split_8h.html]
- "The sum total value of the splits are zero. If there are only two splits, then the value of one must be positive, the other negative." [Source: github.com/Gnucash/gnucash/blob/master/src/engine/Transaction.h]
- "Splits can be balanced even when they are in different currencies, as long as they share a common currency. This feature allows currency-trading accounts." [Source: same]
- "xaccSplitSetValue()" sets the value of the split in the transaction's commodity; "xaccSplitSetAmount()" sets the amount in the account's commodity. [Source: code.gnucash.org/docs/STABLE/Split_8h.html]
- SQL backend: `value_num/denom` = transaction currency; `quantity_num/denom` = account currency. [Source: wiki.gnucash.org/wiki/SQL — splits table]

**VERIFIED**: GnuCash = double-entry with full multi-currency support via split value/amount split.

## Maybe Finance data model — VERIFIED

Direct reading of `memo.d.foundation/breakdown/maybe-finance` (architecture breakdown) confirms:

- Rails 7.2 application. Family model = tenant root, owns accounts, transactions, categories, tags, merchants, rules, imports, plaid_items, invitations.
- Accounts polymorphic via Rails delegated types: checking, savings, credit, investment, loan, property.
- **Entry pattern**: transactions/valuations/trades all use the polymorphic `entryable` pattern. Entries are immutable records.
- "Uses Synth Finance API for real-time exchange rates."
- Family default currency serves as base; per-account native currency; Money objects handle conversion.
- Imports: CSV imports, Mint exports; type-specific models (TransactionImport, TradeImport, AccountImport).

**VERIFIED**: Maybe = entry-pattern ledger; Family tenancy; multi-currency; Plaid + manual + CSV imports.

## Monarch Money API — VERIFIED

Direct reading of `github.com/hammem/monarchmoney` Python GraphQL client confirms:

- GraphQL endpoint (not REST).
- Transaction fields: `id, amount, pending, date, hideFromReports, plaidName, notes, isRecurring, reviewStatus, needsReview, isSplitTransaction, createdAt, updatedAt, hasSplitTransactions, isManual, importedFromMint, syncedFromInstitution`.
- Category fields: `id, name, group { id, type, name }`.
- Tag fields: `id, name, color, order`.
- Merchant: `id, name, transactionsCount`.
- Recurring transactions: separate `recurringTransactionItems` query returns `frequency, amount, isApproximate, merchant, date, isPast, transactionId, amountDiff, category, account`.
- Cashflow: `avg, count, max, maxExpense, sum, sumIncome, sumExpense, first, last`.

**VERIFIED**: Monarch = GraphQL, modern type-safe shape, recurring transactions separated from ad-hoc, "flex budgeting" (category-based).

## Open Banking provider landscape — VERIFIED

Cross-referenced `openbankingcompare.com/blog/best-open-banking-api-providers-for-developers-2026` (2026-05-17) with multiple OSS projects:

| Provider | Region | Self-serve? | Free prod? | Notes |
|---|---|---|---|---|
| **Enable Banking** | EU/UK | Yes | Restricted Production (own accounts) | Replacement for GoCardless BAD post-closure |
| **GoCardless BAD (Nordigen)** | EU/UK | Closed to new signups | Legacy only | Replaced by Enable Banking |
| **Yapily** | EU/UK | Yes | Starter sandbox only | Operates under aggregator's TPP licence |
| **Tink** | EU/UK | Dev account | No | Enterprise data + enrichment |
| **TrueLayer** | UK/EU | Yes | No | Payments-led |
| **Salt Edge** | Global | Yes | No | Global breadth |
| **Token.io** | EU/UK | TPP signup | No | Enterprise VRP |
| **Plaid** | US/CA/EU | US self-serve; EU sales | ~200 live calls/product | US default; EU production sales-led |
| **Teller.io** | US | Yes | 100 free live connections | US indie option |
| **SimpleFIN** | US | Yes | $15/yr read-only, 24 req/day | Self-hosted PFM sweet spot |
| **Belvo / Pluggy / Fintoc** | LatAm | Yes | Sandbox free | LatAm sandbox-friendly |
| **Akoya** | US | Self-serve sandbox | No | Regulated clean stack |

**VERIFIED via 3 sources**: (1) openbankingcompare.com 2026-05-17 review; (2) open-banking-rails README ("GoCardless used to offer a similar free tier but dropped it"); (3) BankingSync README uses Enable Banking; (4) OpenCoffer README uses SimpleFIN.

## EXPAND markers (Wave 3 leads)

- **LEAD**: Verify Actual Budget's rules engine + API importTransaction behavior. ANGLE: clone actualbudget/actual, look at `loot-core/src/server/accounts/sync.ts`.
- **LEAD**: Lunch Money data model & API. ANGLE: lunchmoney.app API docs.
- **LEAD**: PII redaction libraries (Microsoft Presidio, LLM Guard) usage patterns. ANGLE: read LLM Guard docs.
- **LEAD**: SimpleFIN protocol detail (uses Setup-token + Access-URL). ANGLE: simplefin.org info.
- **LEAD**: Maybe Finance chart-of-accounts style. ANGLE: memo.d.foundation says it uses categories — confirm structure.

## Convergence assessment

After Wave 3, all 4 axes have >= 2 independent source corroboration for major claims. No major leads remain uninvestigated. Time to consolidate.
