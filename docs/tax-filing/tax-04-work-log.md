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
