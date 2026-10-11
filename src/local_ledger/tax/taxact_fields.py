"""Required TaxAct field coverage: prove every needed figure is checked (TAX-08).

The readiness evaluator gates on scope, pending evidence, open questions and
recorded comparison keys. What it deliberately does **not** prove — see its
own docstring — is that *every TaxAct field the working papers require* has a
current, matched comparison against *today's* amounts. That is this module's
single job, and it closes the gap the TAX-06 integration review named:

- a comparison recorded against an older working-paper amount is
  ``SOURCE_DRIFT`` (re-review), not coverage;
- a required figure with no recorded TaxAct entry is ``NOT_RECORDED`` — a
  coverage gap, not an empty success;
- a recorded entry for a figure no working paper produces is ``ORPHANED`` —
  a typo, a rename, or evidence that disappeared;
- a figure with no evidence behind it (amount zero, no members) is
  ``NOT_REQUIRED`` — nobody must enter "0 = 0" comparisons for fields the
  year never produced.

Sources of required fields are the three audit tracks: investments and
rentals expose their own comparison rows; California adjustments are
recomputed from ``CaliforniaTaxService.compute_schedule_ca_adjustments``
and matched against recorded items here. All reads are read-only; recording
or replacing entries stays with the reviewer through
``TaxWorkspaceService.record_reconciliation_item``.

Coverage is complete only when every required field is ``COVERED`` and
nothing is ``ORPHANED``. String statuses by design (see the pdf-worker log
for the StrEnum identity trap).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from local_ledger.tax.investments import (
    DISCREPANCY as INV_DISCREPANCY,
)
from local_ledger.tax.investments import (
    MATCHED as INV_MATCHED,
)
from local_ledger.tax.investments import (
    NOT_RECORDED as INV_NOT_RECORDED,
)
from local_ledger.tax.investments import (
    ORPHANED as INV_ORPHANED,
)
from local_ledger.tax.investments import (
    SOURCE_DRIFT as INV_DRIFT,
)
from local_ledger.tax.investments import (
    InvestmentAuditService,
)
from local_ledger.tax.models import ReconciliationItem, ReconciliationStatus
from local_ledger.tax.rentals import (
    DISCREPANCY as RENT_DISCREPANCY,
)
from local_ledger.tax.rentals import (
    MATCHED as RENT_MATCHED,
)
from local_ledger.tax.rentals import (
    NOT_RECORDED as RENT_NOT_RECORDED,
)
from local_ledger.tax.rentals import (
    ORPHANED as RENT_ORPHANED,
)
from local_ledger.tax.rentals import (
    SOURCE_DRIFT as RENT_DRIFT,
)
from local_ledger.tax.rentals import (
    RentalAuditService,
)

#: Unified verdicts for a required field.
COVERED = "COVERED"
DISCREPANCY = "DISCREPANCY"
SOURCE_DRIFT = "SOURCE_DRIFT"
NOT_RECORDED = "NOT_RECORDED"
ORPHANED = "ORPHANED"
NOT_REQUIRED = "NOT_REQUIRED"

#: The section prefix agent C's California service records items under.
CALIFORNIA_SECTION_PREFIX = "California"


@dataclass(frozen=True)
class FieldCoverage:
    """One TaxAct field's coverage verdict, with its evidence."""

    tax_section: str
    field_key: str
    working_paper_cents: int | None
    taxact_entered_cents: int | None
    delta_cents: int | None
    verdict: str
    note: str
    member_ids: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.verdict != NOT_REQUIRED

    @property
    def blocks_final(self) -> bool:
        return self.verdict != NOT_REQUIRED and self.verdict != COVERED


@dataclass
class CoverageReport:
    """Every field verdict plus the summary an export or packet needs."""

    rows: list[FieldCoverage] = field(default_factory=list)
    california_evaluated: bool = True
    california_note: str = ""

    @property
    def complete(self) -> bool:
        return not any(row.blocks_final for row in self.rows)

    @property
    def gaps(self) -> list[FieldCoverage]:
        return [row for row in self.rows if row.blocks_final]

    def counts(self) -> dict[str, int]:
        by: dict[str, int] = {}
        for row in self.rows:
            by[row.verdict] = by.get(row.verdict, 0) + 1
        return by


def coverage_report(session: Session, workspace_id: str) -> CoverageReport:
    """Recompute TaxAct field coverage from today's working papers.

    Read-only. California adjustments are recomputed best-effort: that
    service needs residency/property facts that may legitimately be
    unrecorded early on, in which case the report says so instead of
    failing the whole coverage view.
    """
    report = CoverageReport()

    investment_rows = InvestmentAuditService(session, workspace_id).comparisons()
    for row in investment_rows:
        report.rows.append(_row(
            row.tax_section, row.field_key, row.working_paper_cents,
            row.taxact_entered_cents, row.delta_cents, row.status, row.note,
            row.member_ids,
        ))

    rental_rows = RentalAuditService(session, workspace_id).comparisons()
    for row in rental_rows:
        report.rows.append(_row(
            row.tax_section, row.field_key, row.working_paper_cents,
            row.taxact_entered_cents, row.delta_cents, row.status, row.note,
            row.member_ids,
        ))

    _california_rows(session, workspace_id, report)
    return report


_STATUS_TO_VERDICT = {
    INV_MATCHED: COVERED, RENT_MATCHED: COVERED,
    INV_DISCREPANCY: DISCREPANCY, RENT_DISCREPANCY: DISCREPANCY,
    INV_DRIFT: SOURCE_DRIFT, RENT_DRIFT: SOURCE_DRIFT,
    INV_NOT_RECORDED: NOT_RECORDED, RENT_NOT_RECORDED: NOT_RECORDED,
    INV_ORPHANED: ORPHANED, RENT_ORPHANED: ORPHANED,
}


def _row(tax_section, field_key, wp_cents, entered_cents, delta, status, note, member_ids) -> FieldCoverage:  # type: ignore[no-untyped-def]
    verdict = _STATUS_TO_VERDICT.get(status, status)
    if verdict == NOT_RECORDED and not wp_cents and not member_ids:
        # No evidence produced this figure this year: nothing to enter, and
        # requiring a "0 = 0" comparison would be ceremony, not coverage.
        verdict = NOT_REQUIRED
        note = "no evidence produced this figure for this year"
    return FieldCoverage(
        tax_section=tax_section,
        field_key=field_key,
        working_paper_cents=wp_cents,
        taxact_entered_cents=entered_cents,
        delta_cents=delta,
        verdict=verdict,
        note=note,
        member_ids=tuple(member_ids or ()),
    )


def _california_rows(session: Session, workspace_id: str, report: CoverageReport) -> None:
    """Coverage for California Schedule CA lines, from agent C's service."""
    try:
        from local_ledger.tax.california import CaliforniaTaxService

        ca = CaliforniaTaxService(session)
        adjustments = ca.compute_schedule_ca_adjustments(workspace_id)
    except Exception as exc:
        report.california_evaluated = False
        report.california_note = (
            f"California adjustments could not be computed yet ({type(exc).__name__}); "
            "record residency and property facts to include Schedule CA coverage"
        )
        return

    recorded = {
        (i.tax_section, i.field_key): i
        for i in session.exec(
            select(ReconciliationItem)
            .where(ReconciliationItem.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        ).all()
        if i.status != ReconciliationStatus.STALE
        and i.tax_section.startswith(CALIFORNIA_SECTION_PREFIX)
    }

    for adj in adjustments.adjustments:
        section = "California > Schedule CA (540)"
        field_key = f"schedule_ca:{adj.topic_id.lower()}"
        expected = int(adj.net_adjustment_cents)
        item = recorded.pop((section, field_key), None)
        if item is None:
            report.rows.append(_row(
                section, field_key, expected, None, None, NOT_RECORDED,
                "no reviewed TaxAct entry recorded for this California adjustment yet",
                (),
            ))
            continue
        if item.source_working_paper_cents != expected:
            report.rows.append(_row(
                section, field_key, expected, item.taxact_entered_cents, None,
                SOURCE_DRIFT,
                (
                    "the working paper changed since this entry was recorded "
                    f"({item.source_working_paper_cents} then, {expected} now)"
                ),
                (),
            ))
            continue
        delta = item.taxact_entered_cents - expected
        status = INV_MATCHED if delta == 0 else INV_DISCREPANCY
        report.rows.append(_row(
            section, field_key, expected, item.taxact_entered_cents, delta, status,
            str(item.explanation or "").strip()
            or ("TaxAct matches the reviewed working paper" if delta == 0
               else f"TaxAct differs by ${abs(delta) / 100:,.2f}"),
            (),
        ))

    for (section, field_key), item in sorted(recorded.items()):
        report.rows.append(_row(
            section, field_key, None, item.taxact_entered_cents, None, ORPHANED,
            "a current comparison exists for this key but the working paper no "
            "longer produces it",
            (),
        ))
