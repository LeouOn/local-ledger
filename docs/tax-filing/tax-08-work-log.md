# TAX-08 work log — TaxAct input comparison and evidence packet

Agent E, October 1, 2026. Status: **complete** (coverage gate + export +
comparison page; navigation wiring handed to the coordinator).

Owned files:

- `src/local_ledger/tax/taxact_fields.py` (new) — required-field coverage
- `src/local_ledger/tax/export.py` (new) — versioned export: manifest,
  CSV checklist, readable packet, staleness verification
- `app_pages/tax_compare.py` (new) — comparison screen + entry recording +
  export buttons (boot-tested; nav line handed off)
- `tests/unit/tax/test_tax_export.py` (new, 15 tests)
- Two precise edits to my own audit modules so reviewers' recorded
  explanations travel through: `tax/investments.py` and `tax/rentals.py`
  (comparison rows now prefer `ReconciliationItem.explanation` over the
  synthesized note; investments comparison members are per-key precise so
  an empty term is `NOT_REQUIRED`, not a mandatory 0 = 0 entry)
- `docs/tax-filing/tax-08-work-log.md` (this file) and the TAX-08 row of
  `docs/tax-filing/tasks.md`

No file owned by another agent was edited. TAX-07 (agent F) is running
concurrently on `tax/extraction.py` per
[the split prompt](PROMPT-extraction.md); neither task touches the other's
files.

## What was built

### 1. The stronger coverage gate (`taxact_fields.py`)

The TAX-06 integration review left one boundary explicitly open: the
service gate checks *recorded comparison keys*; it does not prove that every
required TaxAct field has a comparison or that the recorded amount matches
today's working paper. `coverage_report()` closes exactly that:

- Required fields come from the three audit tracks: investments' and
  rentals' own comparison rows, plus California Schedule CA lines recomputed
  from agent C's `compute_schedule_ca_adjustments` (guarded: if residency /
  property facts are unrecorded, the report says so instead of failing).
- Verdict per field: `COVERED` (current, matched, today's amount) /
  `DISCREPANCY` (exact delta) / `SOURCE_DRIFT` (paper moved since the entry
  was recorded) / `NOT_RECORDED` (coverage gap) / `ORPHANED` (an entry for a
  figure no paper produces) / `NOT_REQUIRED` (no evidence produced this
  figure — nobody records 0 = 0 ceremony).
- Coverage is complete only when every required field is `COVERED` and
  nothing is `ORPHANED`.

### 2. The export (`export.py`)

An export is a private directory outside the repo (0o700; files 0o600):
`manifest.json`, `taxact_inputs.csv`, `packet.md`.

- **Drafts can never masquerade as approved**: drafts are always allowed and
  labelled in all three artifacts (manifest flag, CSV column, packet
  watermark "DRAFT — NOT FOR FILING"). A final export is refused with every
  gap named (readiness reasons + per-field coverage verdicts) and nothing is
  half-written.
- **Approved exports pin the exact reviewed snapshot**: the manifest's
  `content_hash` covers the exported substance (coverage rows with
  citations, document inventory, questions, readiness) and deliberately
  excludes generation timestamps — the same reviewed state always hashes and
  exports byte-identically. `verify_export_current()` recomputes against
  the live workspace: changed evidence ⇒ `stale: true`, reported, never
  silently passing.
- **Traceability**: every row carries member ids (evidence row ids) and the
  citations (document + page + box) of the exact lines beneath it.
- **CSV cells are inert**: any cell that could start a formula
  (`= + - @`, tab, CR) is apostrophe-prefixed — reviewer-entered field keys
  and explanations are untrusted text.
- **No raw documents, no private paths**: the inventory lists id, name,
  type and a 12-hex sha256 prefix; storage paths and home paths never
  appear; full document text is never embedded.

### 3. The comparison page (`app_pages/tax_compare.py`)

Workspace selector; readiness + coverage badges and reasons; the full field
table (working paper / TaxAct entered / delta / verdict); a form that
records a reviewed TaxAct entry through
`TaxWorkspaceService.record_reconciliation_item` (status computed from the
delta; retained history and explanations come from the service); draft and
final export buttons with refusal reasons surfaced.

## Acceptance evidence (15 tests)

| Criterion | Test |
| --- | --- |
| Every required field matched before coverage is complete; zero-evidence fields not required | `test_coverage_gaps_until_every_required_field_is_matched` |
| Deliberately incorrect TaxAct entry ⇒ focused discrepancy with exact delta | `test_wrong_taxact_entry_is_a_focused_discrepancy` |
| Deterministic export reproduces reviewed totals and sources | `test_export_is_deterministic` (same state ⇒ same hash, byte-identical CSV and packet), `test_clean_workspace_exports_final` (checklist carries the exact figures) |
| Drafts labelled; cannot masquerade as approved | `test_draft_is_allowed_with_gaps_and_labelled_everywhere`, `test_final_refused_with_named_gaps` |
| Missing basis / missing rental support block a final export, by name | `test_missing_basis_blocks_final_with_a_named_reason`, `test_missing_rental_support_blocks_final` |
| Resolved items retain explanation and evidence | `test_resolved_items_retain_explanation_and_evidence` (current row carries the resolving explanation; the superseded explanation stays in the service's STALE history) |
| Formula-like text is safe in CSV | `test_formula_like_text_is_csv_safe` (adversarial key + explanation; asserts no cell starts with a formula character after parsing) |
| Every approved amount traceable to evidence | `test_traceability_every_row_cites_its_evidence` |
| Changed evidence marks the export stale | `test_changed_evidence_marks_an_export_stale` |
| No raw documents / private paths; minimal identifiers | `test_manifest_carries_no_private_paths_or_raw_documents` |
| Destination hygiene | `test_destination_guards` (in-repo refused, existing refused) |
| Page boots | `test_taxact_comparison_page_boots` |

## Deliberate scope notes

- **No TaxAct import compatibility is claimed.** The CSV is a review
  checklist for a human, not an import format; no product/format was
  validated.
- **Recording entries stays with the workspace service** (retained history,
  stale cascade); the export module only reads state and writes files.
- California coverage is best-effort by design: their service requires
  residency/property facts; absence is reported as "not computed yet"
  rather than crashing the coverage view or the export.
- A CLI for exports was not added (the task names a page and exports; the
  page plus the importable `export_packet` cover it). The default export
  root is `LOCAL_LEDGER_TAX_EXPORT_DIR` or
  `~/.local/share/local-ledger/tax_exports/`.

## Handed to the coordinator

- Nav line for `app.py` (the audit-wave pages plus this one can merge in a
  single pass):
  `st.Page("app_pages/tax_compare.py", title="TaxAct Compare", icon=":material/fact_check:")`
- The TAX-15 row said "Not started" while its work was complete; I
  reconciled it to COMPLETED citing agent C's work log (noted in the row).

## Verification at hand-off

- Full default suite: **620 passed, 10 skipped, zero failures**
  (605 before this task; +15 here). `ruff check` clean on all files I
  touched.
- No personal records; all evidence synthetic through the real service
  APIs.
