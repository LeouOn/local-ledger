"""Unit tests for tax workspace backup and restore (TAX-09).

Acceptance coverage:
1. A restored synthetic workspace has identical source hashes, verified
   values, questions and totals.
2. Backup is consistent while WAL is active on the source database.
3. Corrupt or incomplete backups (bad snapshot hash, missing document) fail
   clearly at verify and restore time.
4. Incompatible versions (alembic stamp mismatch, unknown backup format) are
   refused.
5. Restore never overwrites a live workspace silently: existing targets, the
   live application DB and non-empty document stores are refused.
6. Neither backup output nor restored data may land in tracked (repository)
   paths; the manifest carries no private absolute paths.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine, select

from local_ledger.db import REPO_ROOT, init_db
from local_ledger.tax import Jurisdiction, TaxWorkspaceService
from local_ledger.tax import backup as backup_mod
from local_ledger.tax.backup import (
    BackupDestinationError,
    BackupIntegrityError,
    BackupVersionError,
    RestoreTargetError,
    create_backup,
    restore_backup,
    verify_backup,
)
from local_ledger.tax.backup import (
    main as backup_cli,
)
from local_ledger.tax.models import ExtractedField, TaxDocument, TaxQuestion


@dataclass
class SeededFacts:
    """Expected post-restore state, captured from the source before backup."""

    db_path: Path
    storage_root: Path
    workspace_ids: list[str]
    doc_hashes: dict[str, str]
    field_states: dict[str, str]
    question_states: dict[str, str]
    totals: dict[str, int]
    extra: dict[str, object] = field(default_factory=dict)


@pytest.fixture()
def tax_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tax_dir = tmp_path / "private_tax_storage"
    tax_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tax_dir))
    return tax_dir


def _stamp_alembic_version(db: Path) -> str:
    """Give a create_all-style test DB the stamp a migrated app DB carries."""
    head = backup_mod._current_alembic_head()
    assert head, "repository must expose an alembic head for version checks"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        conn.execute("DELETE FROM alembic_version")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (head,))
        conn.commit()
    finally:
        conn.close()
    return head


def _seed_workspace(db_path: Path) -> SeededFacts:
    """Build a synthetic two-year workspace with messy, realistic state."""
    engine: Engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    # WAL is the app's normal journal mode (db.enable_wal); set it on the file
    # so backups are exercised against a WAL database throughout.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    _stamp_alembic_version(db_path)

    with Session(engine) as session:
        svc = TaxWorkspaceService(session)

        ws25 = svc.get_or_create_workspace(2025, Jurisdiction.FEDERAL)
        ws24 = svc.get_or_create_workspace(2024, Jurisdiction.FEDERAL)

        # 2025: original 1099 with a verified field, then a corrected statement
        # that supersedes it — the old amount must stay SUPERSEDED everywhere.
        doc_v1 = svc.register_document(
            workspace_id=ws25.id,
            file_name="1099_v1.pdf",
            source=b"%PDF-1.4 original-1099-v1-evidence",
            doc_type="1099_CONSOLIDATED",
        )
        f_old = svc.propose_field(ws25.id, doc_v1.id, "box1a_dividends", "$1,000.00", 100000)
        svc.verify_field(f_old.id, verified_by="reviewer_jane")
        assert svc.compute_verified_totals(ws25.id) == 100000

        doc_v2 = svc.register_document(
            workspace_id=ws25.id,
            file_name="1099_v2_corrected.pdf",
            source=b"%PDF-1.4 corrected-1099-v2-evidence",
            doc_type="1099_CONSOLIDATED",
            is_corrected=True,
            supersedes_doc_id=doc_v1.id,
        )
        session.refresh(f_old)
        assert f_old.status == "SUPERSEDED"
        assert svc.compute_verified_totals(ws25.id) == 0

        f_new = svc.propose_field(ws25.id, doc_v2.id, "box1a_dividends", "$700.00", 70000)
        svc.verify_field(f_new.id, verified_by="reviewer_jane")
        f_bad = svc.propose_field(ws25.id, doc_v2.id, "box3_interest", "$300.00", 30000)
        svc.reject_field(f_bad.id, rejected_by="reviewer_jane", notes="not on this form")
        assert svc.compute_verified_totals(ws25.id) == 70000

        question = svc.create_tax_question(
            ws25.id, "Is the state tax refund taxable for 2025?", context="Open item"
        )
        svc.record_reconciliation_item(
            workspace_id=ws25.id,
            tax_section="Schedule B",
            field_key="box1a_dividends",
            source_working_paper_cents=70000,
            taxact_entered_cents=70000,
            delta_cents=0,
            status="MATCHED",
        )
        svc.create_evidence_packet(ws25.id, is_draft=True)

        # 2024: independent workspace with its own document and verified field.
        doc24 = svc.register_document(
            workspace_id=ws24.id,
            file_name="2024_carryover.pdf",
            source=b"%PDF-1.4 prior-year-carryover-evidence",
        )
        f24 = svc.propose_field(ws24.id, doc24.id, "capital_loss_carryover", "$420.00", 42000)
        svc.verify_field(f24.id, verified_by="reviewer_jane")
        assert svc.compute_verified_totals(ws24.id) == 42000

        docs = [doc_v1, doc_v2, doc24]
        facts = SeededFacts(
            db_path=db_path,
            storage_root=Path(doc_v1.storage_path).parent.parent,
            workspace_ids=[ws25.id, ws24.id],
            doc_hashes={d.id: d.sha256_hash for d in docs},
            field_states={f.id: f.status for f in (f_old, f_new, f_bad, f24)},
            question_states={question.id: question.status},
            totals={ws25.id: 70000, ws24.id: 42000},
            extra={"ws25": ws25.id, "ws24": ws24.id, "doc_v1_path": doc_v1.storage_path},
        )
    engine.dispose()
    return facts


@pytest.fixture()
def seeded(tmp_path: Path, tax_env: Path) -> SeededFacts:
    return _seed_workspace(tmp_path / "app.db")


# ---------------------------------------------------------------------------
# 1. round trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_workspace_state(seeded: SeededFacts, tmp_path: Path) -> None:
    """Restored workspaces keep hashes, verified values, questions and totals."""
    dest = tmp_path / "backups" / "first"
    created = create_backup(seeded.db_path, dest)
    manifest = json.loads((created / "manifest.json").read_text())
    assert len(manifest["documents"]) == 3
    assert {w["tax_year"] for w in manifest["workspaces"]} == {2025, 2024}

    target_db = tmp_path / "restore" / "app.db"
    target_storage = tmp_path / "restore" / "documents"
    report = restore_backup(created, target_db, target_storage)
    assert report.documents_restored == 3

    engine = create_engine(f"sqlite:///{target_db.as_posix()}")
    try:
        with Session(engine) as session:
            svc = TaxWorkspaceService(session)

            # Workspaces, verified totals and questions survive identically.
            for ws_id, expected_total in seeded.totals.items():
                assert svc.compute_verified_totals(ws_id) == expected_total
            restored_questions = {
                q.id: q.status for q in session.exec(select(TaxQuestion)).all()
            }
            assert restored_questions == seeded.question_states

            # Field-level review state survives, including SUPERSEDED/REJECTED.
            restored_fields = {
                f.id: f.status for f in session.exec(select(ExtractedField)).all()
            }
            assert restored_fields == seeded.field_states

            # Documents keep their identity and content hash, but now live at
            # rewritten absolute paths inside the restored storage root.
            for doc_id, expected_sha in seeded.doc_hashes.items():
                doc = session.get(TaxDocument, doc_id)
                assert doc is not None
                assert doc.sha256_hash == expected_sha
                new_path = Path(doc.storage_path)
                assert str(target_storage) in str(new_path)
                assert new_path.is_file()
                assert backup_mod.compute_sha256(new_path) == expected_sha
                # The superseded original is retained for the audit trail.
                assert new_path.name.endswith(".pdf")
    finally:
        engine.dispose()

    # Restore report repeats the manifest totals for the operator.
    by_year = {w["tax_year"]: w["verified_total_cents"] for w in report.workspaces}
    assert by_year == {2025: 70000, 2024: 42000}


# ---------------------------------------------------------------------------
# 2. WAL-active consistency
# ---------------------------------------------------------------------------


def test_backup_with_wal_active_captures_committed_writes(
    seeded: SeededFacts, tmp_path: Path
) -> None:
    """A commit sitting in the -wal file at backup time is in the snapshot."""
    ws25 = str(seeded.extra["ws25"])
    keeper = sqlite3.connect(seeded.db_path)
    try:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            "INSERT INTO taxquestion (id, workspace_id, question_text, status) "
            "VALUES ('qst-walmarker', ?, 'wal marker question', 'OPEN')",
            (ws25,),
        )
        keeper.commit()
        wal_file = Path(str(seeded.db_path) + "-wal")
        assert wal_file.exists() and wal_file.stat().st_size > 0, (
            "test setup must leave committed pages in the WAL"
        )

        dest = tmp_path / "backups" / "wal-active"
        create_backup(seeded.db_path, dest)
    finally:
        keeper.close()

    target_db = tmp_path / "restore" / "app.db"
    restore_backup(dest, target_db, tmp_path / "restore" / "documents")
    conn = sqlite3.connect(target_db)
    try:
        row = conn.execute(
            "SELECT status FROM taxquestion WHERE id = 'qst-walmarker'"
        ).fetchone()
        assert row is not None, "WAL-committed row missing from snapshot"
        assert row[0] == "OPEN"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. destination and target guards
# ---------------------------------------------------------------------------


def test_backup_destination_guards(seeded: SeededFacts, tmp_path: Path) -> None:
    """Backup output refuses tracked paths, storage root and existing dirs."""
    with pytest.raises(BackupDestinationError, match="inside the repository"):
        create_backup(seeded.db_path, REPO_ROOT / "data" / "should_not_exist")

    with pytest.raises(BackupDestinationError, match="inside the tax document"):
        create_backup(seeded.db_path, seeded.storage_root / "nested_backup")

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BackupDestinationError, match="already exists"):
        create_backup(seeded.db_path, existing)


def test_restore_target_guards(seeded: SeededFacts, tmp_path: Path) -> None:
    """Restore never silently overwrites existing, live or non-empty targets."""
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "g")

    occupied_db = tmp_path / "occupied.db"
    occupied_db.write_bytes(b"existing database")
    with pytest.raises(RestoreTargetError, match="already exists"):
        restore_backup(backup_dir, occupied_db, tmp_path / "s1")

    live = tmp_path / "live-app.db"
    live.write_bytes(b"the running application database")
    original_live = backup_mod.LIVE_DB_PATH
    backup_mod.LIVE_DB_PATH = live
    try:
        with pytest.raises(RestoreTargetError, match="live application database"):
            restore_backup(backup_dir, live, tmp_path / "s2")
    finally:
        backup_mod.LIVE_DB_PATH = original_live

    busy_storage = tmp_path / "busy_storage"
    busy_storage.mkdir()
    (busy_storage / "original.pdf").write_bytes(b"live original")
    with pytest.raises(RestoreTargetError, match="not empty"):
        restore_backup(backup_dir, tmp_path / "fresh2.db", busy_storage)

    with pytest.raises(BackupDestinationError, match="inside the repository"):
        restore_backup(backup_dir, REPO_ROOT / "data" / "restored.db", tmp_path / "s3")

    # Nothing above may have left output behind.
    assert not (tmp_path / "fresh.db").exists()
    assert not (tmp_path / "s2").exists() or not any((tmp_path / "s2").iterdir())


# ---------------------------------------------------------------------------
# 4. corrupt / incomplete backups
# ---------------------------------------------------------------------------


def _writable(path: Path) -> Path:
    path.chmod(0o600)
    return path


def test_corrupt_snapshot_fails_verify_and_restore(seeded: SeededFacts, tmp_path: Path) -> None:
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "c")
    snapshot = _writable(backup_dir / "snapshot.db")
    data = bytearray(snapshot.read_bytes())
    data[len(data) // 2] ^= 0xFF
    snapshot.write_bytes(bytes(data))

    with pytest.raises(BackupIntegrityError, match="snapshot hash mismatch"):
        verify_backup(backup_dir)
    with pytest.raises(BackupIntegrityError, match="snapshot hash mismatch"):
        restore_backup(backup_dir, tmp_path / "r.db", tmp_path / "rs")
    assert not (tmp_path / "r.db").exists(), "failed restore must remove partial output"


def test_missing_document_fails_with_document_identity(
    seeded: SeededFacts, tmp_path: Path
) -> None:
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "m")
    docs = json.loads((backup_dir / "manifest.json").read_text())["documents"]
    victim = backup_dir / docs[0]["backup_rel_path"]
    _writable(victim)
    victim.unlink()

    with pytest.raises(BackupIntegrityError, match=str(docs[0]["id"])):
        verify_backup(backup_dir)


def test_backup_refuses_corrupt_or_missing_original(seeded: SeededFacts, tmp_path: Path) -> None:
    """A source whose stored original no longer matches its hash is refused."""
    docs = list(seeded.doc_hashes)
    victim_path = Path(str(seeded.extra["doc_v1_path"]))
    victim_path.chmod(0o600)
    victim_path.write_bytes(b"tampered evidence bytes")

    dest = tmp_path / "backups" / "bad-source"
    with pytest.raises(BackupIntegrityError, match="no longer matches its recorded SHA-256"):
        create_backup(seeded.db_path, dest)
    assert not dest.exists(), "failed backup must remove partial output"

    victim_path.unlink()
    with pytest.raises(BackupIntegrityError, match="missing from private storage"):
        create_backup(seeded.db_path, dest)
    assert not dest.exists()
    assert docs  # sanity: the seeding produced documents to tamper with


# ---------------------------------------------------------------------------
# 5. version compatibility
# ---------------------------------------------------------------------------


def test_incompatible_alembic_version_refused(seeded: SeededFacts, tmp_path: Path) -> None:
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "v")
    manifest_path = _writable(backup_dir / "manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["source"]["alembic_version"] = "dead0000dead"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupVersionError, match="dead0000dead"):
        restore_backup(backup_dir, tmp_path / "r.db", tmp_path / "rs")

    # Fresh backup + explicit mismatched expectation is also refused.
    fresh = create_backup(seeded.db_path, tmp_path / "backups" / "v2")
    with pytest.raises(BackupVersionError, match="cross-version"):
        restore_backup(fresh, tmp_path / "r2.db", tmp_path / "rs2", expected_alembic_version="other")


def test_unsupported_backup_format_refused(seeded: SeededFacts, tmp_path: Path) -> None:
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "f")
    manifest_path = _writable(backup_dir / "manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["backup_format_version"] = 99
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupVersionError, match="format version"):
        verify_backup(backup_dir)


def test_backup_without_alembic_stamp_refused(seeded: SeededFacts, tmp_path: Path) -> None:
    """A DB that was never migrated has no stamp; its backup cannot validate."""
    unstamped = tmp_path / "unstamped.db"
    src_conn = sqlite3.connect(seeded.db_path)
    dst_conn = sqlite3.connect(str(unstamped))
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    conn = sqlite3.connect(unstamped)
    conn.execute("DROP TABLE alembic_version")
    conn.commit()
    conn.close()

    dest = tmp_path / "backups" / "u2"
    create_backup(unstamped, dest)
    with pytest.raises(BackupVersionError, match="no alembic version stamp"):
        restore_backup(dest, tmp_path / "r.db", tmp_path / "rs")


# ---------------------------------------------------------------------------
# 6. privacy of output
# ---------------------------------------------------------------------------


def test_manifest_carries_no_private_paths(seeded: SeededFacts, tmp_path: Path) -> None:
    backup_dir = create_backup(seeded.db_path, tmp_path / "backups" / "p")
    text = (backup_dir / "manifest.json").read_text()
    assert str(seeded.storage_root) not in text
    assert str(Path.home()) not in text
    assert "_source_path" not in text
    manifest = json.loads(text)
    assert "plaintext" in manifest["protection"]["at_rest"]

    modes = {
        "backup_dir": backup_dir.stat().st_mode & 0o777,
        "snapshot": (backup_dir / "snapshot.db").stat().st_mode & 0o777,
        "manifest": (backup_dir / "manifest.json").stat().st_mode & 0o777,
    }
    assert modes["backup_dir"] == 0o700
    assert modes["snapshot"] == 0o600
    assert modes["manifest"] == 0o600


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------


def test_cli_create_verify_restore_round_trip(
    seeded: SeededFacts, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "cli_backup"
    assert backup_cli(["create", "--db", str(seeded.db_path), "--dest", str(dest)]) == 0
    assert (dest / "manifest.json").is_file()

    assert backup_cli(["verify", "--backup", str(dest)]) == 0

    target_db = tmp_path / "cli_restore" / "app.db"
    rc = backup_cli(
        [
            "restore",
            "--backup",
            str(dest),
            "--target-db",
            str(target_db),
            "--target-storage",
            str(tmp_path / "cli_restore" / "docs"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2025 US-FED" in out and "70000" in out

    # A corrupt backup fails the CLI with a non-zero exit code.
    snapshot = _writable(dest / "snapshot.db")
    data = bytearray(snapshot.read_bytes())
    data[0] ^= 0xFF
    snapshot.write_bytes(bytes(data))
    capsys.readouterr()
    assert backup_cli(["verify", "--backup", str(dest)]) == 1
    assert "error:" in capsys.readouterr().out
