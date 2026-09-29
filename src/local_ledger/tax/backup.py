"""Tax workspace backup and restore (TAX-09).

A backup is a self-contained directory in a private destination:

    <backup-dir>/
      manifest.json          format version, alembic version, hashes, totals
      snapshot.db            consistent SQLite snapshot of the whole app DB
      documents/<year>/...   immutable copies of every referenced original

Design decisions worth knowing before relying on this module:

- **Whole-DB snapshot, not a table export.** Review history, verification
  events, audit rows and the ledger the evidence points at are all part of the
  record; a workspace exported without them cannot be audited. The snapshot is
  taken with SQLite's online backup API, which is consistent even while the
  source has WAL active (the app's normal mode).
- **Originals are re-hashed at backup time.** A backup must never package a
  corrupt or missing original silently: if a stored document no longer matches
  its recorded SHA-256, the backup fails loudly instead of archiving bad
  evidence.
- **Restore targets must be fresh.** Restoring never writes over the live
  database or a live document store, and never merges into an existing one.
  Restore into a separate target first, inspect it, then swap deliberately
  while the app is closed. That is the whole point of "restore cannot
  overwrite a live workspace silently".
- **Versions are validated, not assumed.** The manifest records the alembic
  version of the source; restore refuses a backup whose version does not
  match the current head. A restore across schema versions needs a deliberate
  migration path, not a hopeful file copy.
- **At-rest protection is permissions only.** The backup is a plaintext copy
  of private data: directory mode 0o700, snapshot/manifest 0o600, documents
  0o400. There is no encryption or authentication. Protecting the destination
  volume (full-disk encryption, offline media, access control) is the user's
  responsibility and is recorded in the manifest so the assumption travels
  with the backup.

Neither backup output nor restored data may land inside the repository
(tracked paths); destinations and targets are refused there. Paths printed
in errors are masked via :func:`local_ledger.tax.storage.sanitize_path`.

CLI::

    python -m local_ledger.tax.backup create  [--db PATH] [--dest DIR]
    python -m local_ledger.tax.backup verify  --backup DIR
    python -m local_ledger.tax.backup restore --backup DIR \
        --target-db PATH --target-storage DIR
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from local_ledger.db import DB_PATH, REPO_ROOT
from local_ledger.tax.storage import compute_sha256, get_tax_storage_dir, sanitize_path

BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = "snapshot.db"
DOCUMENTS_DIR = "documents"

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]")

# Module-level so tests can point "the live DB" at a throwaway path.
LIVE_DB_PATH = DB_PATH


class TaxBackupError(Exception):
    """Base error for tax backup/restore failures."""


class BackupDestinationError(TaxBackupError):
    """Destination is inside tracked paths/storage, or already exists."""


class BackupIntegrityError(TaxBackupError):
    """A hash mismatch or missing file means the backup cannot be trusted."""


class BackupVersionError(TaxBackupError):
    """Backup format or alembic version is incompatible with this code."""


class RestoreTargetError(TaxBackupError):
    """Target exists, is live, is non-empty, or sits in a tracked path."""


@dataclass(frozen=True)
class RestoreReport:
    """What a completed restore produced, for display and for tests."""

    target_db: Path
    target_storage_root: Path
    alembic_version: str
    documents_restored: int
    workspaces: list[dict[str, object]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_backup_root() -> Path:
    import os

    override = os.environ.get("LOCAL_LEDGER_TAX_BACKUP_DIR")
    if override:
        return Path(override).resolve()
    return Path.home() / ".local" / "share" / "local-ledger" / "tax_backups"


def _require_outside_repo(path: Path, role: str) -> None:
    """Refuse paths inside the repository: no private data in tracked paths."""
    resolved = path.resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return
    raise BackupDestinationError(
        f"{role} path {sanitize_path(resolved)} is inside the repository; "
        "private tax data and backup output must stay outside tracked paths"
    )


def _require_outside_storage_root(path: Path, role: str) -> None:
    resolved = path.resolve()
    root = get_tax_storage_dir().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise BackupDestinationError(
        f"{role} path {sanitize_path(resolved)} is inside the tax document "
        "storage root; a backup must not nest inside the data it backs up"
    )


def _sanitize_name(name: str) -> str:
    return _SAFE_FILENAME_RE.sub("_", Path(name).name)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _read_alembic_version(db_path: Path) -> str | None:
    """Read the alembic stamp from a database file, if it has one.

    A schema created via ``create_all`` (tests) has no stamp; the app's real
    DB is migrated and does. A backup without a stamp cannot be validated
    against a version and is refused at restore time.
    """
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "alembic_version"):
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _current_alembic_head() -> str | None:
    """Return the head revision of the repository's migration scripts."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # pragma: no cover - broken alembic install
        return None


def _collect_tax_state(snapshot: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read documents and workspace summaries from the consistent snapshot.

    Reads the snapshot, never the live DB, so the manifest describes exactly
    the state that was captured — not whatever the live DB became meanwhile.
    """
    if not Path(snapshot).exists():  # pragma: no cover - caller guarantees
        raise BackupIntegrityError("snapshot database missing")
    conn = _connect_ro(snapshot)
    try:
        if not _table_exists(conn, "taxdocument"):
            return [], []
        docs: list[dict[str, object]] = []
        for doc_id, ws_id, year, name, storage_path, sha in conn.execute(
            "SELECT d.id, d.workspace_id, w.tax_year, d.file_name, "
            "d.storage_path, d.sha256_hash "
            "FROM taxdocument d JOIN taxworkspace w ON d.workspace_id = w.id "
            "ORDER BY d.id"
        ):
            layout = f"{year}/{doc_id}_{_sanitize_name(str(name))}"
            docs.append(
                {
                    "id": doc_id,
                    "workspace_id": ws_id,
                    "tax_year": year,
                    "file_name": name,
                    "sha256": sha,
                    # Where the bytes live inside the backup directory tree.
                    "backup_rel_path": f"{DOCUMENTS_DIR}/{layout}",
                    # Where they must land inside a document storage root.
                    "storage_layout_name": layout,
                    # Only used for the copy; never written to the manifest.
                    "_source_path": storage_path,
                }
            )
        workspaces: list[dict[str, object]] = []
        for ws_id, year, jur, status in conn.execute(
            "SELECT id, tax_year, jurisdiction, status FROM taxworkspace ORDER BY id"
        ):
            v_count, v_total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM extractedfield "
                "WHERE workspace_id = ? AND status = 'VERIFIED'",
                (ws_id,),
            ).fetchone()
            q_all, q_open = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) "
                "FROM taxquestion WHERE workspace_id = ?",
                (ws_id,),
            ).fetchone()
            workspaces.append(
                {
                    "id": ws_id,
                    "tax_year": year,
                    "jurisdiction": jur,
                    "status": status,
                    "verified_field_count": v_count,
                    "verified_total_cents": v_total,
                    "question_count": q_all or 0,
                    "question_open_count": q_open or 0,
                }
            )
        return docs, workspaces
    finally:
        conn.close()


def _strip_private_keys(docs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{k: v for k, v in d.items() if not k.startswith("_")} for d in docs]


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def create_backup(
    db_path: Path | str | None = None,
    destination: Path | str | None = None,
) -> Path:
    """Create a backup directory and return its path.

    Fails loudly (nothing half-written) if the destination is unusable, the
    source DB is missing, or any stored original no longer matches its
    recorded hash. All private paths are masked in error messages.
    """
    src = Path(db_path) if db_path is not None else LIVE_DB_PATH
    if not src.is_file():
        raise BackupIntegrityError(f"Source database not found: {sanitize_path(src)}")

    if destination is not None:
        dest = Path(destination)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        dest = _default_backup_root() / f"backup-{stamp}-{uuid.uuid4().hex[:6]}"

    _require_outside_repo(dest, "Backup destination")
    _require_outside_storage_root(dest, "Backup destination")
    if dest.exists():
        raise BackupDestinationError(
            f"Backup destination already exists, refusing to mix backups: {sanitize_path(dest)}"
        )

    dest_created = False
    try:
        dest.mkdir(parents=True, exist_ok=False)
        dest_created = True
        dest.chmod(0o700)

        # 1. Consistent SQLite snapshot (correct under WAL; the API copies
        #    pages under a read transaction, so this is the state as of one
        #    point in time even if the app is writing concurrently).
        snapshot = dest / SNAPSHOT_NAME
        src_conn = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(str(snapshot))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        snapshot.chmod(0o600)
        snapshot_sha = compute_sha256(snapshot)

        # 2. Collect documents/workspaces from the snapshot (consistent view).
        docs, workspaces = _collect_tax_state(snapshot)

        # 3. Copy + re-hash every referenced original. A mismatch means the
        #    workspace's evidence is already corrupt: refuse to archive it.
        for doc in docs:
            source_path = Path(str(doc.pop("_source_path") or ""))
            if not source_path.is_file():
                raise BackupIntegrityError(
                    f"Document {doc['id']} ({doc['file_name']}) is missing from "
                    f"private storage: {sanitize_path(source_path)}; "
                    "cannot back up incomplete evidence"
                )
            actual = compute_sha256(source_path)
            if actual != doc["sha256"]:
                raise BackupIntegrityError(
                    f"Document {doc['id']} ({doc['file_name']}) no longer matches its "
                    f"recorded SHA-256 ({sanitize_path(source_path)}); refusing to "
                    "back up corrupt evidence"
                )
            target = dest / str(doc["backup_rel_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
            target.chmod(0o400)

        # 4. Manifest last: its presence means the backup is complete.
        manifest = {
            "backup_format_version": BACKUP_FORMAT_VERSION,
            "created_at": _utcnow_iso(),
            "source": {
                "kind": "sqlite",
                "snapshot_sha256": snapshot_sha,
                "alembic_version": _read_alembic_version(snapshot),
            },
            "documents": _strip_private_keys(docs),
            "workspaces": workspaces,
            "protection": {
                "at_rest": (
                    "plaintext; no encryption or authentication. Directory mode "
                    "0o700, snapshot/manifest 0o600, documents 0o400. Protect the "
                    "destination volume; it contains full original documents."
                ),
            },
        }
        manifest_path = dest / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest_path.chmod(0o600)
        return dest
    except Exception:
        if dest_created:
            shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# verify / restore
# ---------------------------------------------------------------------------


def _load_manifest(backup_dir: Path) -> dict[str, object]:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupIntegrityError(
            f"Not a backup directory (no {MANIFEST_NAME}): {sanitize_path(backup_dir)}"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as err:
        raise BackupIntegrityError(
            f"Backup manifest is not valid JSON: {sanitize_path(manifest_path)} ({err})"
        ) from err
    if not isinstance(manifest, dict):
        raise BackupIntegrityError("Backup manifest must be a JSON object")
    version = manifest.get("backup_format_version")
    if version != BACKUP_FORMAT_VERSION:
        raise BackupVersionError(
            f"Backup format version {version!r} is not supported by this code "
            f"(expected {BACKUP_FORMAT_VERSION}); a newer backup needs a newer app"
        )
    return manifest


def _verify_hashes(backup_dir: Path, manifest: dict[str, object]) -> None:
    source = manifest.get("source")
    if not isinstance(source, dict) or "snapshot_sha256" not in source:
        raise BackupIntegrityError("Manifest has no snapshot hash; cannot verify")
    snapshot = backup_dir / SNAPSHOT_NAME
    if not snapshot.is_file():
        raise BackupIntegrityError(
            f"Backup snapshot database is missing: {sanitize_path(snapshot)}"
        )
    actual = compute_sha256(snapshot)
    if actual != source["snapshot_sha256"]:
        raise BackupIntegrityError(
            f"Backup snapshot hash mismatch: {sanitize_path(snapshot)} is corrupt"
        )
    docs = manifest.get("documents")
    if not isinstance(docs, list):
        raise BackupIntegrityError("Manifest has no document list; cannot verify")
    for doc in docs:
        if not isinstance(doc, dict):
            raise BackupIntegrityError("Malformed document entry in manifest")
        rel = doc.get("backup_rel_path")
        sha = doc.get("sha256")
        if not isinstance(rel, str) or not isinstance(sha, str):
            raise BackupIntegrityError("Malformed document entry in manifest")
        path = backup_dir / rel
        if not path.is_file():
            raise BackupIntegrityError(
                f"Backup is incomplete: document {doc.get('id')} ({doc.get('file_name')}) "
                f"is missing from the backup: {sanitize_path(path)}"
            )
        if compute_sha256(path) != sha:
            raise BackupIntegrityError(
                f"Backup document {doc.get('id')} ({doc.get('file_name')}) hash mismatch: "
                f"{sanitize_path(path)} is corrupt"
            )


def _check_version_compatibility(manifest: dict[str, object], expected: str | None) -> str:
    source = manifest.get("source")
    stamped = source.get("alembic_version") if isinstance(source, dict) else None
    if stamped is None:
        raise BackupVersionError(
            "Backup has no alembic version stamp; its schema cannot be validated "
            "against this code, so restoring it is refused"
        )
    if expected is None:
        expected = _current_alembic_head()
    if expected is None:  # pragma: no cover - broken alembic install
        raise BackupVersionError("Cannot determine this code's migration head")
    if stamped != expected:
        raise BackupVersionError(
            f"Backup was taken at alembic version {stamped}, but this code is at "
            f"{expected}. Restore into a matching checkout (or migrate deliberately); "
            "a cross-version file copy is not a supported restore path"
        )
    return str(stamped)


def verify_backup(backup_dir: Path | str) -> dict[str, object]:
    """Re-check a backup's manifest, snapshot and document hashes.

    Raises on any problem; returns the manifest summary when everything
    matches. Never touches any database.
    """
    bd = Path(backup_dir)
    manifest = _load_manifest(bd)
    _verify_hashes(bd, manifest)
    return manifest


def restore_backup(
    backup_dir: Path | str,
    target_db: Path | str,
    target_storage_root: Path | str,
    expected_alembic_version: str | None = None,
) -> RestoreReport:
    """Restore a backup into a fresh, separate target.

    The target database must not exist, must not be the live application
    database, and the target storage root must be empty or absent — restore
    never merges into or overwrites live data. Everything is validated
    (manifest, format version, alembic version, snapshot hash, every document
    hash) before anything is written, and the restored database is
    cross-checked against the manifest afterwards; a failed restore removes
    its own partial output.
    """
    bd = Path(backup_dir)
    tdb = Path(target_db)
    troot = Path(target_storage_root)

    # -- validate the backup completely before touching the target ---------
    manifest = _load_manifest(bd)
    _verify_hashes(bd, manifest)
    stamped_version = _check_version_compatibility(manifest, expected_alembic_version)

    # -- validate the target ------------------------------------------------
    _require_outside_repo(tdb, "Restore target database")
    _require_outside_repo(troot, "Restore target storage")
    if tdb.resolve() == LIVE_DB_PATH.resolve():
        raise RestoreTargetError(
            "Refusing to restore over the live application database "
            f"({sanitize_path(tdb)}). Restore into a separate target first, inspect "
            "it, then swap files deliberately while the app is closed."
        )
    if tdb.exists():
        raise RestoreTargetError(
            f"Restore target database already exists: {sanitize_path(tdb)}; "
            "restore creates a fresh target and never overwrites existing data"
        )
    if troot.exists() and any(troot.iterdir()):
        raise RestoreTargetError(
            f"Restore target storage root is not empty: {sanitize_path(troot)}; "
            "restoring into a live document store could overwrite originals"
        )

    docs = manifest["documents"]
    workspaces = manifest.get("workspaces")
    if not isinstance(workspaces, list):
        raise BackupIntegrityError("Manifest has no workspace list; cannot restore")

    wrote_db = False
    wrote_storage = False
    try:
        tdb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bd / SNAPSHOT_NAME, tdb)
        tdb.chmod(0o600)
        wrote_db = True

        troot.mkdir(parents=True, exist_ok=True)
        troot.chmod(0o700)
        wrote_storage = True

        restored_docs = 0
        for doc in docs:
            assert isinstance(doc, dict)
            layout = doc.get("storage_layout_name")
            if not isinstance(layout, str):  # pragma: no cover - manifest validated
                raise BackupIntegrityError("Malformed document entry in manifest")
            target_file = troot / layout
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bd / str(doc["backup_rel_path"]), target_file)
            target_file.chmod(0o400)
            # Verify the copy, not just the source: a corrupt write must not
            # pass silently either.
            if compute_sha256(target_file) != doc["sha256"]:
                raise BackupIntegrityError(
                    f"Restored copy of document {doc.get('id')} failed verification at "
                    f"{sanitize_path(target_file)}"
                )
            restored_docs += 1

        # -- point the restored workspace at its new storage location -------
        conn = sqlite3.connect(str(tdb))
        try:
            for doc in docs:
                assert isinstance(doc, dict)
                new_path = str((troot / str(doc["storage_layout_name"])).resolve())
                conn.execute(
                    "UPDATE taxdocument SET storage_path = ? WHERE id = ?",
                    (new_path, doc["id"]),
                )
            conn.commit()

            # -- cross-check restored state against the manifest ------------
            for ws in workspaces:
                assert isinstance(ws, dict)
                v_count, v_total = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM extractedfield "
                    "WHERE workspace_id = ? AND status = 'VERIFIED'",
                    (ws["id"],),
                ).fetchone()
                if (v_count, v_total) != (
                    ws["verified_field_count"],
                    ws["verified_total_cents"],
                ):
                    raise BackupIntegrityError(
                        f"Restored workspace {ws['id']} does not match the backup "
                        f"manifest (verified fields/totals); restore failed"
                    )
                q_open = conn.execute(
                    "SELECT COUNT(*) FROM taxquestion "
                    "WHERE workspace_id = ? AND status = 'OPEN'",
                    (ws["id"],),
                ).fetchone()[0]
                if q_open != ws["question_open_count"]:
                    raise BackupIntegrityError(
                        f"Restored workspace {ws['id']} question state does not match "
                        "the backup manifest; restore failed"
                    )
        finally:
            conn.close()

        return RestoreReport(
            target_db=tdb,
            target_storage_root=troot,
            alembic_version=stamped_version,
            documents_restored=restored_docs,
            workspaces=list(workspaces),
        )
    except Exception:
        # Remove only what this call created; never touch anything pre-existing.
        if wrote_db and tdb.exists():
            tdb.unlink(missing_ok=True)
        if wrote_storage and troot.exists():
            shutil.rmtree(troot, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m local_ledger.tax.backup",
        description="Back up and restore tax workspaces (TAX-09).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create a backup directory")
    p_create.add_argument("--db", default=None, help="Source SQLite DB (default: app DB)")
    p_create.add_argument(
        "--dest", default=None, help="Backup destination directory (default: private root)"
    )

    p_verify = sub.add_parser("verify", help="Verify a backup's hashes and manifest")
    p_verify.add_argument("--backup", required=True, help="Backup directory")

    p_restore = sub.add_parser("restore", help="Restore a backup into a fresh target")
    p_restore.add_argument("--backup", required=True, help="Backup directory")
    p_restore.add_argument("--target-db", required=True, help="Fresh target DB path")
    p_restore.add_argument("--target-storage", required=True, help="Fresh document store path")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            dest = create_backup(db_path=args.db, destination=args.dest)
            print(f"Backup created: {sanitize_path(dest)}")
            return 0
        if args.command == "verify":
            manifest = verify_backup(args.backup)
            docs = manifest.get("documents", [])
            print(f"Backup verified: {sanitize_path(Path(args.backup))}")
            print(f"  documents: {len(docs) if isinstance(docs, list) else '?'}")
            return 0
        report = restore_backup(
            args.backup,
            target_db=args.target_db,
            target_storage_root=args.target_storage,
        )
        print(f"Restored to: {sanitize_path(report.target_db)}")
        print(f"  documents restored: {report.documents_restored}")
        print(f"  alembic version: {report.alembic_version}")
        for ws in report.workspaces:
            print(
                f"  workspace {ws['tax_year']} {ws['jurisdiction']}: "
                f"{ws['verified_field_count']} verified fields, "
                f"verified total {ws['verified_total_cents']} cents, "
                f"{ws['question_open_count']} open question(s)"
            )
        return 0
    except TaxBackupError as err:
        print(f"error: {err}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
