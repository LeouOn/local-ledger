# TAX-09 work log — tax workspace backup and restore

Agent D, September 29, 2026. Status: **completed**.

Acceptance criteria are documented in
[tasks.md](tasks.md#tax-09--add-tax-workspace-backup-and-restore).

## Context at pickup

TAX-04 persistence was implemented and hardened (four review-found defects
fixed; see the TAX-04 section of tasks.md). TAX-06 was being implemented
concurrently in the same working tree by another agent, with uncommitted edits
to `tax/reconciliation.py`, `tax/classification.py`, `tax/models.py` and a new
migration. This task was therefore scoped to **entirely new files** to keep the
collision risk near zero, per the process lesson recorded in
[COLLISION-2026-09-28.md](COLLISION-2026-09-28.md):

- `src/local_ledger/tax/backup.py` (new)
- `tests/unit/tax/test_tax_backup.py` (new)
- `docs/tax-filing/tax-09-work-log.md` (this file) and the TAX-09 row/section
  of `docs/tax-filing/tasks.md`

No shared schema, service, navigation or settings files were touched. The
task allows "CLI or dedicated settings section"; the CLI was implemented and
the settings section is left for a serialized UI pass, so no shared surface
was edited while other agents were mid-flight.

## Design decisions

1. **Whole-DB snapshot, not a table export.** A backup directory contains a
   consistent SQLite snapshot (`snapshot.db`) taken with SQLite's online
   backup API, plus immutable copies of every referenced document original
   (`documents/<year>/...`), plus a `manifest.json`. Review history,
   verification events, audit rows and the ledger transactions that evidence
   points at are part of the tax record; exporting only tax tables would
   produce a workspace that cannot be audited. The online backup API is
   correct while WAL is active, which is the app's normal journal mode
   (`db.enable_wal`).
2. **The manifest is written last and describes the snapshot, not the live
   DB.** Document and workspace state is collected by querying the snapshot
   itself, so a concurrent ledger write cannot make the manifest lie about
   what was captured. Manifest presence is the "backup is complete" marker;
   every failure path removes the partial directory.
3. **Originals are re-hashed at backup time.** If a stored document is
   missing or no longer matches its recorded SHA-256, the backup fails
   loudly (`BackupIntegrityError`) rather than archiving corrupt evidence.
   A workspace whose sources are already rotting should be repaired, not
   archived as-if.
4. **Restore always targets a fresh location.** `restore_backup` refuses an
   existing target database, refuses the live application database outright
   (with the swap-while-closed procedure in the error), refuses a non-empty
   document store, and refuses tracked/in-repo paths. The intended flow is:
   restore into a separate target, inspect it, then swap deliberately.
   Restore validates everything first (manifest, format version, alembic
   stamp, snapshot hash, every document hash), re-hashes its own copies
   after writing, cross-checks restored verified totals/questions against
   the manifest, and removes its own partial output on any failure.
5. **Version gate, not version hope.** The manifest records the source's
   alembic version; restore refuses when it does not match this checkout's
   head (`BackupVersionError` names both), and refuses backups with no stamp
   at all. A cross-version restore needs a deliberate migration path, not a
   file copy. This also behaves correctly while migrations are landing
   concurrently (TAX-06's `c3d91f6a2b48` is included in the chain the smoke
   test ran).
6. **`storage_path` is rewritten on restore.** `TaxDocument.storage_path` is
   an absolute path, so a restored workspace is re-pointed at the restored
   document store inside the restored database; document bytes and hashes
   are unchanged.
7. **At-rest protection is permissions only, and the manifest says so.**
   Backup directory mode 0o700, snapshot/manifest 0o600, documents 0o400
   (matching the read-only originals policy in `tax/storage.py`). There is
   no encryption or authentication; the manifest's `protection.at_rest` note
   travels with the backup so the assumption is never silent. Protecting
   the destination volume is the user's responsibility.
8. **No private data in tracked paths, none in the manifest.** Destinations
   and restore targets inside the repository are refused; the manifest
   contains no absolute filesystem paths and no raw values, and error
   messages are masked through `sanitize_path`.

## CLI

```
python -m local_ledger.tax.backup create  [--db PATH] [--dest DIR]
python -m local_ledger.tax.backup verify  --backup DIR
python -m local_ledger.tax.backup restore --backup DIR \
    --target-db PATH --target-storage DIR
```

Default source is the application DB; default destination root is
`~/.local/share/local-ledger/tax_backups/` (override:
`LOCAL_LEDGER_TAX_BACKUP_DIR`), each backup in a unique timestamped
subdirectory.

## Acceptance evidence (`tests/unit/tax/test_tax_backup.py`, 12 tests)

| Criterion | Test |
| --- | --- |
| Restored synthetic workspace has identical source hashes, verified values, questions, totals | `test_round_trip_preserves_workspace_state` (two years, superseded + rejected + verified fields, open question, corrected-document lineage) |
| WAL-active backup | `test_backup_with_wal_active_captures_committed_writes` (commit left in `-wal` under an open connection is present in the snapshot) |
| Corrupt backup fails clearly | `test_corrupt_snapshot_fails_verify_and_restore`, `test_missing_document_fails_with_document_identity` (names the document id) |
| Incomplete source fails clearly | `test_backup_refuses_corrupt_or_missing_original` (and removes partial output) |
| Incompatible versions | `test_incompatible_alembic_version_refused`, `test_unsupported_backup_format_refused`, `test_backup_without_alembic_stamp_refused` |
| Restore cannot overwrite live/silent | `test_restore_target_guards` (existing target, the live DB, non-empty store, tracked path) |
| Nothing lands in tracked paths; manifest privacy | `test_backup_destination_guards`, `test_manifest_carries_no_private_paths` (also asserts 0o700/0o600 modes) |
| CLI works end to end | `test_cli_create_verify_restore_round_trip` (exit codes, corrupt backup exits non-zero) |

A subprocess smoke test additionally ran the real CLI against a database
built through the app's actual alembic chain (all eight migrations through
`c3d91f6a2b48`, TAX-06's head) with synthetic documents only: create →
verify → restore all succeeded, and the restore report reproduced the
workspace's verified total and open-question count.

## Verification at hand-off

- `pytest tests/unit -q` → **461 passed, 10 skipped, zero failures**
  (12 of the passed tests are this task's; the rest of the suite —
  including TAX-06's reconciliation tests, which were red mid-session
  while that agent worked — was green at hand-off).
- `ruff check src/local_ledger/tax/backup.py
  tests/unit/tax/test_tax_backup.py` → clean.
- No user data touched: tests and smoke use synthetic bytes in pytest temp
  dirs and `/tmp/opencode`; the real `data/ledger.db` was only inspected
  read-only (it is currently an empty placeholder, so no live backup of it
  was possible or needed).
