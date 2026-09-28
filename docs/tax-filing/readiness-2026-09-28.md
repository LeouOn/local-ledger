# Tax preparation readiness — September 28, 2026

Local Ledger is a functioning personal budgeting foundation, but it is not yet
a dependable tax preparation assistant. The intended milestone is a local, source-linked review of the figures the
user enters into TaxAct, with explanations, discrepancy checks and supporting
records for 2025 federal/California investments and rental income. Implementing tax calculations or electronic filing is a separate scope.

This assessment covers the current working tree, including pre-existing edits
to `llm/local_server.py`, its tests, and the untracked tax-filing templates.
Those files were not changed by this assessment. No personal records or running
model were inspected. This is a software assessment, not a tax treatment review.

## Scope assumptions

The user confirmed **tax year 2025, California, investments and rental income**,
with the goal of auditing TaxAct inputs and understanding correct entry, rather
than replacing TaxAct. Federal/California reconciliation is the working scope.
California residency (full-year/part-year/nonresident), rental property locations,
ownership structure, investment types, filing status and TaxAct Online/Desktop
edition remain to be confirmed. Do not infer full-year residency or that every
rental is in California. These details govern form and screen mappings.

The objective is to reduce avoidable errors and improve documentation; neither
this software nor a model can guarantee that a return will avoid an audit.
Prioritize a manual source-to-TaxAct comparison before automated extraction or
conversational features.

## What exists and what it means

| Area | Evidence in this repository | Readiness for tax work |
| --- | --- | --- |
| Ledger | `models.py`, `ledger/service.py`, `ledger/trends.py`: integer cents, accounts, categories, paired transfers, audit records | Useful substrate; annual tax reconciliation is absent |
| CSV ingestion | `ingest/csv_import.py`, `ingest/importer.py`, `app_pages/import_.py`: parsing, staging and duplicate classification | Integrity defects need correction before trusting imported totals |
| Local inference | `llm/providers.py`, `llm/local_server.py`, `llm/structured.py`: loopback endpoint checks, Ollama/DS4/GLM profiles, structured output and startup management | Plumbing exists; real-model tax accuracy, latency and capacity are unmeasured |
| Model review | `llm/categorize.py`, `llm/paste_parse.py`: proposals and review patterns | Reusable concepts; tax evidence and verification states are missing |
| Budget assistant | `analysis/chat.py`: monthly aggregate context; `ui/chat_sidebar.py`: chat UI | Cannot retrieve tax forms or cite form boxes; summaries round display values to whole dollars |
| Cloud boundary | `llm/privacy.py`, `llm/providers.py`, cloud audit code | Existing consent and aggregate restrictions are a useful baseline; tax documents need a dedicated local-only path |
| Tax workflow | `docs/tax-filing/workflow.md` and three CSV headers | Manual templates only; no tax tables, document ingestion, review page or packet export |
| Quality evidence | Unit suite plus optional integration spikes | Broad budget coverage, no tax benchmark or filing acceptance evidence |

## Confirmed blockers

1. **Import metadata is lost.** `ImportService.commit()` does not carry staged
   payee/category information into the transaction. `PasteParseService` also
   drops proposed payee/category information and writes directly rather than
   sharing the staging/dedup path described in its docstring.
2. **Commit is not idempotent.** Committed staged rows remain available and are
   not linked to their resulting transaction. Repeating the same commit creates
   another transaction. Stage classification only considers already committed
   rows, leaving same-batch and concurrent-stage cases unresolved.
3. **Review is not enforced.** The service skips exact matches but does not
   require acceptance of fuzzy matches or reject rejected rows. The import UI
   submits pending fuzzy rows. The Transactions page has no staged-row review
   implementation despite the import page directing the user there.
4. **Fuzzy matching does not resolve actual payees.** `ledger/dedup.py` reads
   `Transaction.notes` as the payee rather than joining `Payee`. Its tests use
   notes to simulate payees, masking the end-to-end gap.
5. **Local startup tests depend on a machine-specific script.** Seven tests
   fail when `~/Projects/ds4/run-ds4f.sh` is absent, before their fake launcher
   can exercise the intended behavior. This is a test portability/runtime
   configuration gap; it does not establish that inference itself is broken.
6. **The current model quality spike is not an acceptance gate.**
   `tests/integration/test_eval_quality.py` leaves successful outputs unscored,
   consequently chooses `cloud-consent`, and writes settings in the default
   application database. Refactor it before using it to assess local models.
7. **No tax evidence lifecycle exists.** There is no persisted relationship
   among source file, page, form field, extracted value, human verification,
   corrected document, reconciliation and exported amount. There is also no
   app-level backup/restore workflow for such a collection.

## Validation performed

` .venv/bin/python -m pytest tests/unit -q ` on September 28:
**323 passed, 7 failed, 10 skipped**, in 21.69 seconds. All seven failures are in
`test_local_server.py`; failure details point to the missing DS4 launcher.
Skipped tests are not evidence of working behavior.

A synthetic in-memory SQLite check, using no user data, confirmed:

- A CSV row with a payee produces a transaction with `payee_id=None`.
- Committing one staged row twice produces two transactions.
- A pending fuzzy row is committed when its ID is supplied.

Real-model integration tests were not run: the existing quality test mutates
default app settings, and its placeholder scoring cannot establish model
quality. No claim is made about current GPU capacity, available models, OCR
accuracy, or runtime performance.

## Recommended product boundary

Use the local model to propose document labels, extracted fields and possible
transaction classifications, and to explain already computed results with
source references. Use deterministic code for amounts, comparisons and totals.
Retain the original document and require explicit verification before a value
contributes to approved totals. Confidence is not verification.

Tax classifications should be separate from budget categories. Record the
tax year, jurisdiction, supporting facts and authority for treatment decisions;
do not let model memory establish them. Authority collection and year-specific
form rules are follow-up work after scope is known. Unsupported forms should
remain explicit unresolved items, with manual entry available.

The first deliverable is useful without inference: document inventory, manual
verified fields, annual reconciliation, unresolved questions and an export.
Local extraction then reduces typing without becoming a prerequisite for
finishing preparation. This avoids delaying a near-term return on OCR or model
research.

Implementation work is specified in [the task backlog](tasks.md), with explicit
prerequisites, ownership boundaries and acceptance criteria.

## Investment, rental and California priorities

A brokerage cash deposit does not establish capital gain. Preserve proceeds,
basis, adjustments and holding-period/reporting categories as distinct evidence
fields, with corrected statements and missing basis visible. The 2025
[Form 8949 instructions](https://www.irs.gov/instructions/i8949) support the
need to reconcile reported sales and adjustments rather than infer gains from
cash movements.

Rental working papers need per-property income/expense evidence and asset
history, including prior depreciation and unresolved use/allocation questions.
The 2025 [Schedule E instructions](https://www.irs.gov/instructions/i1040se)
and [Publication 527](https://www.irs.gov/publications/p527) are starting
references; applicability depends on the user's property facts.

Retain federal and California values separately. California's 2025
[Schedule CA instructions](https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html)
and [Schedule D instructions](https://www.ftb.ca.gov/forms/2025/2025-540-d-instructions.html)
identify adjustment paths; federal figures must not silently become California
figures. Resident versus nonresident treatment remains a scope question.

These official references were checked September 28, 2026 to ground feature
scope, not to determine this user's tax treatment. Exact TaxAct screen guidance
must be checked for the user's edition and tax year when that task is implemented.
