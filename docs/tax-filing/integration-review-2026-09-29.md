# PDF worker and TAX-06 integration review — September 29, 2026

**Follow-up status:** the five integration findings below and two additional
stale-read cases are now fixed and regression tested. The final default suite
passed **557 tests, 10 skipped, 4 deselected**. See the
[integration-fix handoff](tax-06-integration-fixes-2026-09-29.md) for changes,
verification and the remaining TaxAct mapping/UI boundary. This report retains
its original reproductions as historical evidence.

Reviewed the uncommitted worker and TAX-06 hardening against the agreed design.
The baseline was independently verified: **527 passed, 10 skipped**, Ruff clean
for `src`, `tests`, `app.py` and `app_pages`; Alembic has one head,
`e2b47a9c6d10`. Additional checks used synthetic records and temporary databases.
No real tax records or model calls were used.

## Integration completed in this review

- `pdf_text.extract_pages()` now obtains `pdf_worker.default_extractor()` when
  no extractor is explicitly supplied. Intake therefore uses a worker by
  default; `LOCAL_LEDGER_PDF_EXTRACTOR=inprocess` remains explicit.
- `ExtractionResult.is_scanned` now requires NO_TEXT_LAYER for every inspected
  page. ERROR, TIMED_OUT and uninspected pages do not establish a scan.
- Intake maps a result with no successfully extracted text and extraction
  failures to UNSUPPORTED_TYPE instead of SCANNED_NO_TEXT/TEXT_EXTRACTABLE.
- Added regression coverage for default factory routing, unchanged explicit
  in-process override, no silent fallback on a worker error, and unreadable
  pages' result/intake classification. Existing intake tests exercise the real
  default worker through the integration seam.

The focused PDF/intake gate passed **42 tests**. Full post-integration gate: **533 passed, 10 skipped**, 55.70 seconds. Ruff
passed for `src`, `tests`, `app.py` and `app_pages`. The existing classification
changes have two trailing-whitespace lines reported by `git diff --check`; they
were left with that implementation owner. No classifier code was changed in
this review.

## TAX-06 acceptance remains incomplete

The isolated repro cases have improved, but the following integration failures
are confirmed. A green suite does not yet cover these cases.

### P0: Cached independent sessions bypass the claimed compare-and-set

The head's version is checked against an ORM object, then updated normally.
There is no conditional database update or ORM version column enforcing that
the stored head still has the expected version at write time.

Reproduction on a temporary file-backed database:

1. Session A retains a head at version 2.
2. Session B replaces the decision and commits version 3.
3. Session A submits a different decision with expected_head_version=2.

Session A's request succeeds. The database then has two CLASSIFIED 10000-bp
parts in the same current revision: B's newer review and A's stale review.
Current allocation arithmetic can subsequently fail because shares sum to
20000. The existing concurrency test closes its old reader and opens a fresh
session for the stale request, so its identity map does not reproduce this.

Required fix: database-level compare-and-set (or SQLAlchemy version checking),
failure rollback for the entire revision/invalidation, and a regression retaining
both sessions' loaded head objects. Cover interleaved and simultaneous writes.

### P0: Rental totals ignore persisted property membership

Configured a workspace with account A and property A selected. A verified,
classified 9999-cent expense on unselected property B appeared in
`rental_totals()` as deductible. That method scans workspace expenses and only
filters by its optional argument; it does not enforce scope.property_ids.

Required fix: use configured property membership for approved workspace runs,
enforce UNCONFIGURED/document-only semantics, and label explicit previews that
depart from persisted scope. Check classification eligibility against scope too.

### P0: New scoped ledger records do not invalidate an old complete run

Persisted a BANK_FLOW run for a selected account, then created a new 50000-cent
tax-year transaction through LedgerService. The old run remained CURRENT while
a fresh report had unresolved_count=1. Import commits similarly have no
transaction-change notification. Deletion invalidation alone cannot establish
completeness after new transactions are added.

Required fix: include creates/imports and every supported edit path in the
transactional invalidation boundary. Determine affected workspaces from scope,
including workspaces whose old run never contained the new transaction ID.

### P0: Final packets still accept unconfigured/incomplete workspaces

Confirmed that `create_evidence_packet(is_draft=False)` succeeds with
UNCONFIGURED scope and no open questions. This is the known readiness gap, not
fixed by the isolated TAX-06 tests. It contradicts the resolved hardening
proposal's snapshot-integrity requirements regardless of which task owns export.

Required fix: share a deterministic readiness evaluation between reports and
packets. Include scope, eligible current revisions, pending source evidence,
unresolved subjects and current TaxAct comparisons as applicable. Allow drafts
and refuse final designation with explicit reasons.

### P1: Resolved history is still counted as pending

Rejected a synthetic 1000-cent field; `source_form_totals()` returned
unresolved_count=1. Its pending collection includes every non-VERIFIED status,
including REJECTED and SUPERSEDED history. Rental reconciliation also needs the
same distinction between historical exclusions and active unresolved subjects.

Required fix: pending counts describe current review obligations, not all saved
historical proposals. A current missing replacement remains a separate gap.

## Remaining user workflow

- Show the hash-verified original page beside the field being approved.
- Preserve worker page statuses and memory-limit caveats through intake DTOs
  and the UI, rather than discarding document_note in the tuple adapter.
- Provide scope configuration in the UI and meaningful per-field/type summaries.
- Complete investment/rental/California checks and TaxAct comparison/export.
- Qualify local extraction on synthetic forms before claiming model readiness.

Prior decision history is useful and should remain. A configurable history
retention system and a custom lint extension are lower priority than these
integrity gaps. Use value equality for persisted StrEnum fields and exercise
database round trips in affected tests; enum-constructor identity comparisons
are a different, valid case.
