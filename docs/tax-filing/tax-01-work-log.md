# TAX-01 work log — import integrity and review

Agent A, started and completed September 28, 2026. Status: **complete**.

Acceptance met; the evidence is in [Test evidence](#test-evidence). Two rules
here are load-bearing for the tax work that follows: a committed transaction is
traceable to a staged row and a source line, and the commit service refuses to
write anything the user did not accept.

## Goals

1. **No import silently loses or invents a transaction.** Every committed
   transaction is traceable to a staged row, a source file and a line number,
   and nothing is dropped without a recorded reason.
2. **Commit is idempotent.** Clicking commit twice, replaying a stage ID, or
   two sessions committing overlapping rows produce the ledger exactly once.
3. **Review is enforced by the service, not by the UI.** The database and the
   service refuse to commit a row the user has not confirmed, and refuse to
   commit a rejected row even if a caller asks.
4. **Imported metadata survives.** Payee (resolved to a `Payee` FK), approved
   category and source batch/row provenance reach the committed transaction.
5. **Tax reconciliation can trust the ledger.** This is the substrate TAX-06
   computes annual totals from, so duplicates, transfers and equal-valued
   purchases must behave predictably.

## Contract changes (public surface)

### `ingest/importer.py`

```python
ImportService.stage(rows, account_id, batch_label) -> list[StagedTransaction]
ImportService.list_staged(account_id=None, batch=None, states=None) -> list[StagedTransaction]
ImportService.set_review(staged_ids, state) -> list[StagedTransaction]   # was one id
ImportService.accept(staged_ids) -> list[StagedTransaction]              # convenience
ImportService.reject(staged_ids) -> list[StagedTransaction]              # convenience
ImportService.commit(staged_ids, *, actor, account_id=None, batch=None) -> CommitResult
```

`CommitResult` is a frozen dataclass:

| field | meaning |
| --- | --- |
| `created` | transactions written, in request order |
| `skipped` | `SkippedRow(staged_id, reason)` for every requested row not written |
| `committed_ids` | the staged ids that produced a transaction |

Skip reasons are stable strings: `already-committed`, `rejected`,
`needs-acceptance`, `duplicate-exact`, `out-of-scope`.

Rules enforced in `commit`:

- `committed_transaction_id is not None` → `already-committed`. Committing the
  same ids twice is a no-op that returns the existing transaction ids.
- `review_state is REJECTED` → `rejected`. Never written, under any argument.
- `NEW` or `FUZZY` with `review_state is not ACCEPTED` → `needs-acceptance`.
  Confirmation is the caller's job, deliberately explicit.
- A row that re-classifies to `EXACT` against the live ledger at commit time →
  `duplicate-exact` (re-checked at commit, not only at staging).
- An id outside `account_id`/`batch` scope, or a missing id → raises and rolls
  the whole commit back.

### `ledger/dedup.py`

`classify(candidate, existing, window_days=7, payee_names=None)`. The payee
used for comparison is resolved from `Transaction.payee_id` through
`payee_names`; `Transaction.notes` is **not** a payee. The notes fallback is
kept only for rows with no `payee_id` at all and is documented as such, because
dropping it silently would change classification for existing callers.

`stage` classifies against the committed ledger **and** rows already staged for
the same account (this batch and any other pending batch). A same-batch match
is recorded as `FUZZY`, never `EXACT`: an equal-valued pair of genuine
purchases must reach the review queue rather than be auto-skipped.

### `llm/paste_parse.py`

`commit_proposals` no longer writes transactions directly. It builds
`ParsedTransaction` rows, routes them through `ImportService.stage`, marks the
accepted subset `ACCEPTED` and commits — so paste, CSV and any future source
share one staging, dedup, review and audit path. Payee and suggested category
are carried; unknown category names stay unresolved instead of inventing one.

### `ingest/payees.py` (new)

- `resolve_or_create_payee(session, name) -> Payee` — case-insensitive match on
  `normalized_name`, creates on miss.
- `resolve_category(session, name) -> Category | None` — match only; never
  creates, so an LLM cannot invent budget categories during import.

### Schema / migration (owned by TAX-01, coordinate with TAX-04)

`stagedtransaction` gains `notes`, `source_row` and
`committed_transaction_id` (FK → `transaction.id`). `transaction` gains the
partial unique index `(account_id, imported_id) WHERE imported_id IS NOT NULL`.

The index is created only when existing rows already satisfy it; the migration
logs and skips rather than failing the upgrade or deleting a user's
transactions. The service enforces the same rule without the index, so
correctness never depends on it.

## Test evidence

`.venv/bin/python -m pytest tests/unit -q` on September 28, 2026, in the shared
working tree (which also carries agent B's in-flight TAX-02 changes):
**381 passed, 10 skipped** in ~35s. The 10 skips are the pre-existing ones; the
seven `test_local_server.py` failures recorded in the readiness assessment are
gone, and nothing in this task reintroduced them. `ruff check` is clean over
`src/local_ledger/{ingest,ledger}`, `llm/paste_parse.py`, `models.py`,
`app_pages/` and `tests/`; the only lint errors in the tree are in
`llm/local_server.py`, which this task does not own.

Acceptance coverage:

| Criterion | Where |
| --- | --- |
| repeated clicks | `test_repeated_commit_of_same_ids_writes_once` |
| repeated stage ids | `test_repeated_ids_in_one_commit_write_once` |
| same-batch duplicates | `test_same_batch_duplicate_is_fuzzy_not_exact`, `test_second_batch_sees_uncommitted_rows_as_fuzzy` |
| reimport with/without external ids | `test_reimport_with_external_ids_produces_no_new_rows`, `test_reimport_without_external_ids_asks_instead_of_writing` |
| concurrent commits | `test_two_sessions_committing_the_same_row_write_once` |
| rejected / pending fuzzy rows | `test_rejected_row_is_never_committed`, `test_rejection_is_terminal`, `test_pending_fuzzy_row_is_not_committed_without_acceptance` |
| payee + category retention | `test_commit_retains_payee_and_approved_category`, `test_commit_resolves_payee_name_to_a_foreign_key`, `test_commit_proposals_keeps_payee_and_matching_category` |
| rollback of transaction *and* audit | `test_commit_is_atomic_on_failure` |
| conflict re-check at commit | `test_commit_rechecks_conflicts_against_the_live_ledger` |
| commit scoping | `test_commit_rejects_rows_from_another_batch`, `..._another_account`, `..._unknown_id` |
| equal-valued purchases kept | `test_equal_valued_purchases_both_survive_review` |
| payee resolved by FK, not notes | `test_stage_marks_fuzzy_via_payee_foreign_key`, `test_stage_does_not_treat_a_note_as_a_payee`, `test_notes_are_not_a_payee` |
| migration + index, duplicate-safe upgrade | `tests/unit/test_migrations.py` (4 new cases) |
| UI journey reviews and commits a subset | `tests/unit/test_import_review_journey.py` |

The UI journey was verified to fail when the review rule is removed (committing
every pending row was injected as a bug and the journey failed), so it is not a
smoke test that passes whatever the page does.

## Notes for the other agents

- **TAX-04 (schema owner):** rebase onto `e5f7a1c9d204`. It adds
  `stagedtransaction.{notes,source_row,committed_transaction_id,committed_at,committed_by}`
  and the partial unique index `uq_transaction_account_imported_id`. The index
  is skipped when a workspace already holds duplicate `(account_id,
  imported_id)` rows, so an existing database may not have it - do not assume
  its presence in a test.
- **TAX-06 (reconciliation):** transactions created by import carry
  `payee_id`, `category_id`, `imported_id`, `notes` and `created_by` (e.g.
  `import:bank.csv`), and every committed row links back to its staged row
  through `committed_transaction_id`. A re-checked conflict is never written,
  so committed totals do not double count. A `Transfer` still creates its
  mirrored pair outside the import path, unchanged.
- **TAX-02 (runtime):** this task added no changes to
  `llm/local_server.py`; the uncommitted edits there are still yours.
- Both agents are editing one working tree. Nothing in this task was committed,
  and no file owned by TAX-02 was touched. My files are listed in the handoff
  summary.

