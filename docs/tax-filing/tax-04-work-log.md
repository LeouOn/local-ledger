# TAX-04 work log — persist tax workspace and evidence lifecycle

Agent B, started September 28, 2026. Status: **completed**.

This log records the implementation of database persistence, evidence lifecycle state machines,
and Alembic migrations for the tax workspace contract established in TAX-03.
Acceptance criteria are in [tasks.md](tasks.md#tax-04--persist-tax-workspace-and-evidence-lifecycle).

## Goals & Deliverables

1. **Implement TAX-03 schema and models.** Defined SQLModel tables in `src/local_ledger/tax/models.py`
   for:
   - `TaxWorkspace`: annual preparation workspace partitioned by `tax_year` and `jurisdiction`.
   - `TaxDocument`: immutable metadata and private storage tracking with SHA-256 hash.
   - `ExtractedField`: proposed, verified, rejected, or superseded field amounts with exact integer cents and raw text.
   - `TaxProperty`: rental real estate property records for Schedule E.
   - `RentalExpenseRecord`: operating expense items linked to property and source evidence or ledger transactions.
   - `AssetDepreciation`: asset depreciation schedules under federal MACRS and California rules.
   - `InvestmentSaleRecord`: security disposition records for Form 8949 / Schedule D audit.
   - `JurisdictionCarryover`: federal and California prior-year carryovers.
   - `TaxQuestion`: unresolved tax treatment questions.
   - `ReconciliationItem`: audited comparisons between source working papers and TaxAct entries with consumed transaction tracking.
   - `EvidencePacket`: audit evidence packet snapshot with content hash.
2. **Re-export and registration in `src/local_ledger/models.py`.**
   Registered all 11 models on `SQLModel.metadata` without modifying existing budgeting tables.
3. **Alembic migration `f6a8b2c4d305_add_tax_workspace_and_evidence_tables.py`.**
   - Cleanly chained onto Agent A's head migration `e5f7a1c9d204`.
   - `upgrade()` creates all 11 tables and indexes cleanly.
   - `downgrade()` drops all 11 tables cleanly.
   - Verified that existing budgeting tables, accounts, and transactions are preserved intact during upgrade.
4. **Private Document Storage (`src/local_ledger/tax/storage.py`).**
   - Storage root configured via `LOCAL_LEDGER_TAX_DIR` (defaults to `~/.local/share/local-ledger/tax_documents/`).
   - Copies files immutably with read-only permissions (0o400).
   - Re-verifies file integrity against SHA-256 hash on demand.
   - Masks sensitive paths and PII (SSNs, account numbers) via `mask_pii` and `sanitize_path` to prevent leakage into error logs or exceptions.
5. **Tax Workspace Service (`src/local_ledger/tax/service.py`).**
   - `get_or_create_workspace(tax_year, jurisdiction)` ensuring year isolation.
   - `register_document(...)` handling immutable storage, hash deduplication, and supersession invalidation cascades.
   - Field verification lifecycle: `propose_field`, `verify_field`, `reject_field`.
   - Approval invariant: `compute_verified_totals` strictly includes ONLY `VERIFIED` fields. `PROPOSED`, `REJECTED`, or `SUPERSEDED` records are strictly ignored.
   - Invalidation cascades:
     - Registering a corrected document marks prior fields `SUPERSEDED` and marks downstream `ReconciliationItem` and `EvidencePacket` as `STALE`.
     - Ledger transaction edits trigger `invalidate_for_transaction_edit(transaction_id)` to mark affected reconciliation items and evidence packets `STALE`.
   - Workspace locking: prevents modifications to locked workspaces with `WorkspaceLockedError`.
6. **Comprehensive Unit & Migration Tests.**
   - `tests/unit/tax/test_tax_models.py`: 7 tests verifying integer cents validation (floats/bools rejected), ISO date formats, default fields, and enum consistency.
   - `tests/unit/tax/test_tax_service.py`: 9 tests verifying year isolation, storage integrity, approval invariant, supersession cascades, transaction invalidation, locking, and error masking.
   - `tests/unit/test_migrations.py`: 3 tests added verifying tax table creation on upgrade, clean downgrade to `e5f7a1c9d204`, and preservation of pre-existing budgeting transactions.

## Verification Results

- All 26 tax and migration unit tests pass (`pytest tests/unit/tax/ tests/unit/test_migrations.py`).
- Full unit test suite passes: 400 passed, 10 skipped in 40.33s.
- Ruff linter check passes with zero errors (`ruff check src/local_ledger/tax/ tests/unit/tax/ ...`).

---

# Hardening pass — September 28, 2026

A review of this implementation (400 passing tests, but the suite was asserting
the wrong things in four places) found defects that no existing test covered.
All four are fixed, with regression tests in
`tests/unit/tax/test_tax_hardening.py`. Unit suite after the pass: **420 passed,
10 skipped**.

Each defect was reproduced before it was fixed, and each fix was verified by
reverting the fix and confirming the matching test fails — a test that passes
both with and without the fix proves nothing.

## 1. Evidence could cross tax years

`propose_field(workspace_id, document_id, ...)` trusted both arguments. A field
in the 2024 workspace could name a 2025 document, be verified, and land in the
2024 totals. `_handle_supersession` then selected by `document_id` alone, so
correcting the 2025 document also marked the 2024 field `SUPERSEDED` - one
correction silently mutating another year's evidence.

Fixed by `_require_document_in_workspace`, which refuses a document that does not
exist, belongs to another workspace or year, or has itself been superseded
(`WorkspaceScopeError` / `DocumentNotFoundError`). `register_document` now also
refuses to supersede a document outside its own workspace, and the supersession
cascade is explicitly scoped to the document's own workspace.

Note this is enforced in the service, not by a foreign key. A FK would not have
caught it: both rows exist, they are simply in different workspaces, and SQLite
FK enforcement is off by default in this app.

## 2. Superseded or rejected evidence could be reapproved

`verify_field` set any field to `VERIFIED` regardless of its prior status, so a
field that had been superseded by a corrected document could be flipped back and
re-enter an approved total - the exact outcome supersession exists to prevent.

Fixed by `_require_reviewable`: only `PROPOSED` may be reviewed, on fields,
rental expenses and investment sales alike (`InvalidStateTransitionError`).
Re-proposing the value against the current document is the way back, which keeps
the old decision visible instead of overwriting it.

## 3. Real ledger deletion left tax comparisons marked matched

`invalidate_for_transaction_edit` worked when called, and its unit test called it
directly - so the test passed while the feature was dead. No ledger mutation path
ever called it: `LedgerService.delete_transaction` deleted the row and left every
comparison that consumed it marked `MATCHED`.

Fixed with `ledger/invalidation.py` and a call from `delete_transaction`, inside
the existing transaction, so the deletion and the stale marking commit together
or not at all. The session is passed explicitly rather than discovered, and
`invalidate_for_transaction_edit` gained `commit=False` for that purpose. The
tests now go through `LedgerService.delete_transaction` and additionally cover
the transfer-mirror case, where deleting one leg removes two rows and both must
invalidate.

## 4. A non-draft packet could be created with unresolved questions

`create_evidence_packet(is_draft=False)` had no readiness gate, so an unfinished
workspace could be given the designation that says the review is done. A packet
is a claim about review state, and that claim was unearned.

Fixed with a `PacketReadinessError` when any question is still `OPEN`. Drafts
remain available and are still labelled drafts.

## 5. Two further defects found while fixing the above

- **A correction could be silently dropped.** `register_document` checks for an
  existing document with the same hash and returns it *before* the supersession
  branch runs. Re-uploading identical bytes as a "correction" therefore returned
  the original, with the correction flag never set and the replaced evidence left
  live. Corrections must now differ from what they replace, and the duplicate
  check runs after the supersession validation.
- **Unconfirmed facts were asserted.** A new workspace defaulted to
  `residency_status=FULL_YEAR` and `filing_status=SINGLE`. Neither has been
  confirmed by the user, and a default that reads as a fact is indistinguishable
  from a confirmed one everywhere downstream. Both now default to `UNKNOWN`, with
  the enum values added. No migration is needed: the columns have no server
  default, so existing rows keep whatever was actually recorded.

## Test changes worth noting

Three pre-existing tests asserted the buggy behaviour and were corrected rather
than worked around: two asserted the `FULL_YEAR`/`SINGLE` defaults, and several
proposed fields against made-up document ids like `"doc-1"`, which only worked
because the service never checked. They now register a real synthetic document
first, which is what the invariant requires.

## Artifacts and Owned Files

- `src/local_ledger/tax/__init__.py`
- `src/local_ledger/tax/models.py`
- `src/local_ledger/tax/storage.py`
- `src/local_ledger/tax/service.py`
- `src/local_ledger/models.py`
- `alembic/versions/f6a8b2c4d305_add_tax_workspace_and_evidence_tables.py`
- `tests/unit/tax/test_tax_models.py`
- `tests/unit/tax/test_tax_service.py`
- `tests/unit/test_migrations.py`
- `docs/tax-filing/tax-04-work-log.md`
- `docs/tax-filing/tasks.md`
