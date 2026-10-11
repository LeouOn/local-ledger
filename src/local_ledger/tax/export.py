"""Versioned TaxAct export: manifest, entry checklist and readable packet (TAX-08).

An export is a directory in a private destination outside the repository:

    <destination>/
      manifest.json       format + workspace identity, content hash, coverage
      taxact_inputs.csv   the entry checklist (field, section, amounts, status)
      packet.md           the readable review packet with citations

Rules that shape it, each from the task's acceptance criteria:

**Drafts can never masquerade as approved.** A draft is always allowed and
always labelled — in the manifest, in the CSV, and as a watermark on the
packet. A final export is refused unless review readiness passes AND every
required TaxAct field is covered by a current, matched comparison
(:mod:`local_ledger.tax.taxact_fields`). Refusal lists every gap by name.

**Approved exports identify the exact reviewed snapshot.** The manifest
carries a ``content_hash`` over the exported substance (coverage rows,
evidence citations, document inventory, questions, readiness reasons) —
explicitly excluding generation timestamps. Deterministic: the same reviewed
state always hashes and exports identically. :func:`verify_export_current`
recomputes the hash against the live workspace so changed evidence is
detectable: a stale export says so instead of quietly passing.

**Every approved amount is traceable to its evidence.** Each row carries the
working-paper amount, the TaxAct-entered amount, the verdict, and the
citations of the exact income lines / sale lots / expense records beneath
it. Member ids are evidence row ids; citations are document + page + box.

**CSV cells are inert.** Field text comes from documents and free-form
explanations, so any cell that could start a formula (``= + - @``, tab, CR)
is prefixed with an apostrophe. Formula-like text is data, never code.

**No raw documents, no private paths.** The document inventory lists id,
name, type and a 12-hex sha256 prefix — enough to identify, not enough to
leak. Storage paths, home paths and full document text stay out of the
export; citations carry only the locating snippet context.

Recording or replacing TaxAct entries is a reviewer action
(``TaxWorkspaceService.record_reconciliation_item``, which retains stale
history); this module only reads and writes files.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from local_ledger.db import REPO_ROOT
from local_ledger.tax import taxact_fields
from local_ledger.tax.investments import InvestmentAuditService
from local_ledger.tax.models import TaxDocument, TaxWorkspace
from local_ledger.tax.readiness import evaluate_readiness
from local_ledger.tax.rentals import RentalAuditService
from local_ledger.tax.storage import compute_sha256, sanitize_path

EXPORT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
CSV_NAME = "taxact_inputs.csv"
PACKET_NAME = "packet.md"

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class TaxExportError(Exception):
    """Export was refused or is unusable."""


class ExportReadinessError(TaxExportError):
    """A final (non-draft) export was requested with unresolved gaps."""


@dataclass(frozen=True)
class ExportResult:
    destination: Path
    is_draft: bool
    content_hash: str
    field_count: int


def default_export_root() -> Path:
    override = os.environ.get("LOCAL_LEDGER_TAX_EXPORT_DIR")
    if override:
        return Path(override).resolve()
    return Path.home() / ".local" / "share" / "local-ledger" / "tax_exports"


def _require_outside_repo(path: Path, role: str) -> None:
    try:
        path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return
    raise TaxExportError(
        f"{role} path {sanitize_path(path)} is inside the repository; exported "
        "tax data must stay outside tracked paths"
    )


# ---------------------------------------------------------------------------
# state assembly (shared by export and verification)
# ---------------------------------------------------------------------------


def _workspace(session: Session, workspace_id: str) -> TaxWorkspace:
    ws = session.get(TaxWorkspace, workspace_id)
    if ws is None:
        raise TaxExportError(f"workspace {workspace_id} not found")
    return ws


def _evidence_citations(session: Session, workspace_id: str) -> dict[str, str]:
    """member id -> human citation, from the audit tracks' own papers."""
    citations: dict[str, str] = {}
    investments = InvestmentAuditService(session, workspace_id).working_paper()
    for line in investments.income_lines:
        citations[line.field_id] = line.citation
    for sale in investments.sales:
        citations[sale.sale_id] = sale.citation
    rentals = RentalAuditService(session, workspace_id).working_papers()
    for paper in rentals:
        for line in paper.expense_lines:
            citations[line.expense_id] = line.citation
        for line in paper.income_lines:
            citations[line.field_id] = line.citation
    return citations


def _document_inventory(session: Session, workspace_id: str) -> list[dict[str, object]]:
    docs = session.exec(
        select(TaxDocument).where(TaxDocument.workspace_id == workspace_id)
    ).all()
    return [
        {
            "id": d.id,
            "file_name": d.file_name,
            "doc_type": d.doc_type,
            "sha256_prefix": (d.sha256_hash or "")[:12],
            "is_corrected": bool(d.is_corrected),
            "supersedes": d.supersedes_doc_id,
        }
        for d in sorted(docs, key=lambda d: d.id)
    ]


def _open_questions(session: Session, workspace_id: str) -> list[dict[str, str]]:
    from local_ledger.tax.models import TaxQuestion

    rows = session.exec(
        select(TaxQuestion).where(
            TaxQuestion.workspace_id == workspace_id, TaxQuestion.status == "OPEN"
        )
    ).all()
    return sorted(
        (
            {"id": q.id, "question": q.question_text, "context": q.context or ""}
            for q in rows
        ),
        key=lambda q: q["id"],
    )


def _audit_questions(session: Session, workspace_id: str) -> list[dict[str, str]]:
    """Statement-level audit questions (data, not TaxQuestion rows)."""
    out: list[dict[str, str]] = []
    investments = InvestmentAuditService(session, workspace_id).working_paper()
    for q in investments.questions:
        out.append({"track": "investments", "code": q.code, "question": q.message, "citation": q.citation})
    for paper in RentalAuditService(session, workspace_id).working_papers():
        for q in paper.questions:
            out.append(
                {
                    "track": f"rentals:{paper.label}",
                    "code": q.code,
                    "question": q.message,
                    "citation": q.citation,
                }
            )
    return sorted(out, key=lambda q: (q["track"], q["code"], q["question"]))


def _export_state(session: Session, workspace_id: str) -> dict[str, object]:
    """The canonical exported substance: everything except timestamps."""
    ws = _workspace(session, workspace_id)
    coverage = taxact_fields.coverage_report(session, workspace_id)
    readiness = evaluate_readiness(session, workspace_id)
    citations = _evidence_citations(session, workspace_id)

    rows = []
    for row in sorted(coverage.rows, key=lambda r: (r.tax_section, r.field_key)):
        rows.append(
            {
                "tax_section": row.tax_section,
                "field_key": row.field_key,
                "working_paper_cents": row.working_paper_cents,
                "taxact_entered_cents": row.taxact_entered_cents,
                "delta_cents": row.delta_cents,
                "verdict": row.verdict,
                "note": row.note,
                "member_ids": list(row.member_ids),
                "citations": sorted(
                    {citations[m] for m in row.member_ids if m in citations}
                ),
            }
        )

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "workspace": {
            "id": ws.id,
            "tax_year": ws.tax_year,
            "jurisdiction": ws.jurisdiction,
        },
        "coverage": {
            "complete": coverage.complete,
            "counts": coverage.counts(),
            "california_evaluated": coverage.california_evaluated,
            "california_note": coverage.california_note,
        },
        "readiness": readiness.to_snapshot(),
        "fields": rows,
        "documents": _document_inventory(session, workspace_id),
        "open_questions": _open_questions(session, workspace_id),
        "audit_questions": _audit_questions(session, workspace_id),
    }


def _content_hash(state: dict[str, object]) -> str:
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return compute_sha256(canonical.encode("utf-8"))


# ---------------------------------------------------------------------------
# CSV / packet rendering
# ---------------------------------------------------------------------------


def _csv_safe(cell: object) -> str:
    text = "" if cell is None else str(cell)
    if text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _render_csv(state: dict[str, object], is_draft: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["tax_section", "field_key", "working_paper", "taxact_entered",
         "delta", "verdict", "is_draft", "note"]
    )
    for row in state["fields"]:  # type: ignore[index,union-attr]
        writer.writerow(
            [
                _csv_safe(row["tax_section"]),
                _csv_safe(row["field_key"]),
                _csv_safe(_cents(row["working_paper_cents"])),
                _csv_safe(_cents(row["taxact_entered_cents"])),
                _csv_safe(_cents(row["delta_cents"])),
                _csv_safe(row["verdict"]),
                "DRAFT" if is_draft else "FINAL",
                _csv_safe(row["note"]),
            ]
        )
    return buffer.getvalue()


def _cents(cents: object) -> str | None:
    if cents is None:
        return None
    return f"${int(cents) / 100:,.2f}"


def _render_packet(state: dict[str, object], is_draft: bool) -> str:
    ws = state["workspace"]  # type: ignore[index]
    coverage = state["coverage"]  # type: ignore[index]
    lines: list[str] = []
    banner = (
        "*DRAFT — NOT FOR FILING. Unresolved or unverified content is included "
        "and labelled below.*"
        if is_draft
        else "*Approved export. Identifies the exact reviewed snapshot via "
        "manifest content hash.*"
    )
    lines += [
        f"# TaxAct review packet — {ws['tax_year']} {ws['jurisdiction']}",
        "",
        banner,
        "",
        "This packet audits inputs to TaxAct. TaxAct computes the return; this "
        "workspace computes and reviews the figures that go into it.",
        "",
        "## Coverage",
        "",
        f"- Coverage complete: **{coverage['complete']}**",
        f"- Verdicts: {coverage['counts']}",
    ]
    if not coverage["california_evaluated"]:
        lines.append(f"- {coverage['california_note']}")
    lines += ["", "## Readiness", ""]
    readiness = state["readiness"]  # type: ignore[index]
    if readiness["ready"]:
        lines.append("- Review readiness: **READY**")
    else:
        lines.append("- Review readiness: **NOT READY** — reasons:")
        lines += [f"  - {reason}" for reason in readiness["reasons"]]

    lines += ["", "## TaxAct entry checklist", ""]
    current_section = None
    for row in state["fields"]:  # type: ignore[index,union-attr]
        if row["tax_section"] != current_section:
            current_section = row["tax_section"]
            lines += [f"### {current_section}", ""]
        delta = (
            ""
            if row["delta_cents"] is None
            else f" (delta {_cents(row['delta_cents'])})"
        )
        lines.append(
            f"- **{row['field_key']}** — working paper "
            f"{_cents(row['working_paper_cents']) or '—'}, TaxAct "
            f"{_cents(row['taxact_entered_cents']) or 'not entered'}{delta}: "
            f"{row['verdict']}. {row['note']}"
        )
        for citation in row["citations"]:
            lines.append(f"  - source: {citation}")

    questions = state["open_questions"]  # type: ignore[index]
    audit_q = state["audit_questions"]  # type: ignore[index]
    if questions or audit_q:
        lines += ["", "## Unresolved questions", ""]
        for q in questions:
            lines.append(f"- [{q['id']}] {q['question']}")
        for q in audit_q:
            lines.append(f"- [{q['track']}/{q['code']}] {q['question']} ({q['citation']})")

    lines += [
        "",
        "## Document inventory",
        "",
        "Identified by id, name, type and hash prefix; originals stay in "
        "private storage and are never embedded here.",
        "",
    ]
    for doc in state["documents"]:  # type: ignore[index]
        corrected = " (corrected)" if doc["is_corrected"] else ""
        lines.append(f"- {doc['file_name']}{corrected} — {doc['doc_type']} — {doc['sha256_prefix']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# export / verify
# ---------------------------------------------------------------------------


def export_packet(
    session: Session,
    workspace_id: str,
    *,
    is_draft: bool,
    destination: Path | str | None = None,
) -> ExportResult:
    """Write the export directory. Drafts always allowed; finals must be clean."""
    ws = _workspace(session, workspace_id)
    if destination is not None:
        dest = Path(destination)
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        import uuid

        kind = "draft" if is_draft else "final"
        dest = (
            default_export_root()
            / f"{ws.tax_year}-{ws.jurisdiction.value}-{kind}-{stamp}-{uuid.uuid4().hex[:6]}"
        )
    _require_outside_repo(dest, "Export destination")
    if dest.exists():
        raise TaxExportError(
            f"Export destination already exists: {sanitize_path(dest)}"
        )

    state = _export_state(session, workspace_id)

    if not is_draft:
        problems = _final_blockers(state)
        if problems:
            raise ExportReadinessError(
                "A final export is refused while gaps remain:\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\nExport a draft (labelled as such) or resolve the gaps."
            )

    content_hash = _content_hash(state)
    manifest = dict(state)
    manifest["is_draft"] = is_draft
    manifest["content_hash"] = content_hash
    manifest["generated_at"] = datetime.now(UTC).isoformat()

    created = False
    try:
        dest.mkdir(parents=True, exist_ok=False)
        created = True
        dest.chmod(0o700)

        manifest_path = dest / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest_path.chmod(0o600)

        csv_path = dest / CSV_NAME
        csv_path.write_text(_render_csv(state, is_draft))
        csv_path.chmod(0o600)

        packet_path = dest / PACKET_NAME
        packet_path.write_text(_render_packet(state, is_draft))
        packet_path.chmod(0o600)

        return ExportResult(
            destination=dest,
            is_draft=is_draft,
            content_hash=content_hash,
            field_count=len(state["fields"]),  # type: ignore[arg-type]
        )
    except Exception:
        if created:
            import shutil

            shutil.rmtree(dest, ignore_errors=True)
        raise


def _final_blockers(state: dict[str, object]) -> list[str]:
    problems: list[str] = []
    readiness = state["readiness"]  # type: ignore[index]
    if not readiness["ready"]:
        problems.extend(f"readiness: {reason}" for reason in readiness["reasons"])
    coverage = state["coverage"]  # type: ignore[index]
    if not coverage["complete"]:
        for row in state["fields"]:  # type: ignore[index,union-attr]
            if row["verdict"] not in (taxact_fields.COVERED, taxact_fields.NOT_REQUIRED):
                problems.append(
                    f"coverage: {row['tax_section']} / {row['field_key']} is "
                    f"{row['verdict']} ({row['note']})"
                )
    return problems


def verify_export_current(
    session: Session, workspace_id: str, export_dir: Path | str
) -> dict[str, object]:
    """Compare an export's recorded snapshot against the live workspace.

    Returns a report; never raises on staleness (staleness is information).
    Raises only for a malformed export directory.
    """
    directory = Path(export_dir)
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise TaxExportError(
            f"Not an export directory (no {MANIFEST_NAME}): {sanitize_path(directory)}"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as err:
        raise TaxExportError(
            f"Export manifest is not valid JSON: {sanitize_path(manifest_path)} ({err})"
        ) from err
    if manifest.get("format_version") != EXPORT_FORMAT_VERSION:
        raise TaxExportError(
            f"Export format version {manifest.get('format_version')!r} is not "
            f"supported (expected {EXPORT_FORMAT_VERSION})"
        )

    recorded_hash = manifest.get("content_hash")
    current_state = _export_state(session, workspace_id)
    current_hash = _content_hash(current_state)
    return {
        "is_draft": bool(manifest.get("is_draft")),
        "recorded_content_hash": recorded_hash,
        "current_content_hash": current_hash,
        "stale": recorded_hash != current_hash,
        "field_count": len(manifest.get("fields", [])),
    }
