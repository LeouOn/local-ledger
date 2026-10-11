# TAX-13 work log — investment statement and TaxAct audit

Agent E, October 1, 2026. Status: **complete** (service + tests + read-only
view; navigation wiring handed to the coordinator).

Owned files:

- `src/local_ledger/tax/investments.py` (new)
- `app_pages/tax_investments.py` (new; not yet in `app.py`'s page list — see
  "Handed to the coordinator")
- `tests/unit/tax/test_tax_investments.py` (new, 17 tests)
- `docs/tax-filing/tax-13-work-log.md` (this file) and the TAX-13 row/section
  of `docs/tax-filing/tasks.md`

No file owned by another agent was edited. The TAX-15 agent landed
`tax/california.py`, `app_pages/tax_california.py` and tests while this work
was in flight; their two failing tests and their lint errors (including an
unused `sanitize_path` import they added to the shared `tax/__init__.py`)
are theirs — flagged below, not touched.

## What was built

`InvestmentAuditService(session, workspace_id)` — **read-only**:

- `working_paper()` — the reviewed investment working paper from verified
  source-form evidence only: canonical income keys for 1099-INT/DIV boxes
  (alias table accepts both fixture-style and short manual-entry field
  names; verified-but-unrecognised fields are surfaced, never silently
  dropped), Form 8949 lot detail with covered/noncovered status, dates,
  term, wash-sale disallowance, net-after-wash beside the gross figure, and
  exact totals per term.
- `comparisons()` — every expected investment figure vs the recorded
  current TaxAct entry: MATCHED / DISCREPANCY (exact delta, direction) /
  SOURCE_DRIFT (the paper moved since the entry was recorded) /
  NOT_RECORDED / ORPHANED (an entry exists for a key the paper no longer
  produces — typos and renames become visible).

### Rules that shaped it (each maps to a test)

| Rule | Test |
| --- | --- |
| Missing basis is UNKNOWN, not zero: proceeds count, basis becomes an unresolved amount with a cited question, no gain is invented | `test_working_paper_from_synthetic_consolidated_1099` (GAMMA lot: proceeds 310,000c counted, basis/gain excluded, question says "UNKNOWN, not zero") |
| Wash-sale adjustments are preserved and questioned, never resolved by this service | same test (BETA lot: wash 32,000c, net ST −58,000c vs reported −90,000c, WASH_SALE_REVIEW cites the replacement-purchase review) |
| Reported figures are cross-checked: gain must equal proceeds − basis | `test_reported_gain_must_equal_proceeds_minus_basis` (ARITHMETIC_MISMATCH renders exact dollar amounts) |
| Summary/detail duplicate reporting is flagged; summaries must cross-foot their lots | `test_summary_detail_duplicate_is_flagged`, `test_summary_that_does_not_crossfoot_is_flagged` |
| Identical lots on two statements raise a cross-account question (transfer vs duplicate import) | `test_identical_lot_on_two_statements_is_a_cross_account_question` |
| Qualified dividends / capital-gain distributions cannot exceed ordinary dividends | `test_qualified_dividends_cannot_exceed_ordinary` |
| Corrected statement lineage: superseded evidence excluded (fields AND sales — the service cascade covers both), lineage stated in `correction_notes` | `test_corrected_statement_excludes_superseded_evidence` |
| **Gains never derive from bank flows**: deposits/withdrawals of ±$300k–$500k move nothing, and the module structurally cannot import ledger rows or classifications | `test_gains_never_derive_from_bank_flows`, `test_module_reads_no_bank_flow_tables` (AST-level import check) |
| A missing prior return is a question, not a zero carryover | `test_missing_prior_return_is_a_question_not_zero` |
| Proposed fields stay visible and excluded; unmapped verified fields surface | `test_proposed_fields_are_pending_not_counted` |
| Comparisons are exact and recomputed from today's paper | `test_comparisons_matched_discrepant_and_not_recorded`, `test_comparison_flags_source_drift`, `test_orphaned_comparison_key_is_surfaced` |
| Consistency with TAX-06: the paper's sale totals equal `ReconciliationService.investment_totals()` measures by name, given the same reviewed decisions | `test_totals_agree_with_reconciliation_measures` |
| The view boots against a migrated empty DB | `test_investment_working_paper_page_boots` |

The consolidated fixture scenario produces these exact working-paper
figures (integer cents): interest box1 142,550 / box3 25,000; dividends 1a
385,000 / 1b 290,000 / 2a 45,000 / box7 8,500; proceeds ST 420,000, LT
1,560,000; reported basis ST 510,000, LT 980,000; reported gain ST −90,000,
LT 270,000; net after wash ST −58,000; unknown-basis proceeds 310,000
(1 lot).

## Design decisions

- **Read-only service.** The audit never mutates the workspace: recording or
  replacing a TaxAct entry is a reviewer action
  (`TaxWorkspaceService.record_reconciliation_item`, with its own stale
  history). Questions are returned as data (`AuditQuestion` with code,
  message, citation), not auto-persisted — an audit run must be repeatable
  without spamming `TaxQuestion` rows.
- **Two layers, both kept.** The working paper reads verified statement
  evidence directly; TAX-06 reconciliation counts sale measures through
  reviewed classification decisions (amounts read from the source). They
  agree by name when the same decisions exist — pinned by test. The paper is
  the statement-level audit; reconciliation is the reviewed-decision layer.
- **Comparison statuses are plain strings**, deliberately avoiding the
  StrEnum-vs-plain-string identity trap that has bitten this repo twice
  (noted in the pdf-worker log; a repo lint rule is still the open fix).
- **Canonical keys** (`dividends.box1a`, `proceeds_short_term`, …) double as
  the comparison `field_key`s, so TAX-08 can bind every required TaxAct
  field to a working-paper figure without a translation layer. Sale-total
  key names match reconciliation measure names exactly.

## Handed to the coordinator

1. **One navigation line** in `app.py`'s page list, when the TAX-14/TAX-15
   pages land together (the page file is complete and boots):
   `st.Page("app_pages/tax_investments.py", title="Tax Investments", icon=":material/trending_up:")`
   — left out deliberately: `app.py` is the one file all three audit tracks
   would collide on.
2. **TAX-15 agent's in-flight state** (their files, not mine):
   `test_tax_california.py` has 2 failures in the full suite (1 fails in
   isolation); `app_pages/tax_california.py` and their test file have ruff
   errors (I001/F401/B007/SIM102); their edit to the shared
   `tax/__init__.py` left an unused `sanitize_path` import (F401). Suite
   with only my work and theirs: 581 passed, 10 skipped, 2 failed.

## Verification at hand-off

- My files: `ruff check` clean.
- Full default suite: **581 passed, 10 skipped** + the 2 TAX-15 in-flight
  failures noted above. Before this track: 557 passed, 10 skipped, 4
  deselected. My additions: 17 tests (16 service + 1 page boot).
- No personal records used; all evidence is synthetic, built through the
  real service APIs (`register_document`, `propose_field`/`verify_field`,
  `record_investment_sale`/`verify_investment_sale`,
  `record_reconciliation_item`).
