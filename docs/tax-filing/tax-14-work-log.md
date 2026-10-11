# TAX-14 work log — rental working papers and TaxAct audit

Agent E, October 1, 2026. Status: **complete** (service + tests + read-only
view; navigation wiring handed to the coordinator).

Owned files:

- `src/local_ledger/tax/rentals.py` (new)
- `app_pages/tax_rentals.py` (new; not in `app.py`'s page list — see handoff)
- `tests/unit/tax/test_tax_rentals.py` (new, 22 tests)
- `docs/tax-filing/tax-14-work-log.md` (this file) and the TAX-14 row/section
  of `docs/tax-filing/tasks.md`

No file owned by another agent was edited. No schema changes (the task
forbids them without TAX-04 owner coordination; none were needed —
`TaxProperty`, `RentalExpenseRecord`, `AssetDepreciation` and
`JurisdictionCarryover` already carry everything the audit requires).

## What was built

`RentalAuditService(session, workspace_id)` — **read-only**:

- `working_papers()` / `working_paper(property_id)` — one paper per property:
  verified rent income (attributed via the property's statement documents),
  verified expenses with citations, separated not-an-expense lines,
  depreciation history, suspended-loss carryovers, correction lineage,
  and totals. `fully_checked` is true only with **zero questions and the
  property inside the configured review scope**.
- `comparisons()` — every Schedule E figure vs the current recorded TaxAct
  entry: MATCHED / DISCREPANCY (exact delta, direction) / SOURCE_DRIFT /
  NOT_RECORDED / ORPHANED, with member ids tracing each figure to its
  evidence.

### Acceptance mapping (each scenario has a test)

| Task requirement | Behaviour | Test |
| --- | --- | --- |
| Two properties | figures stay attributed to their own property; rents attributed via the property's statement documents | `test_two_properties_keep_figures_separate` |
| Mortgage principal | separated line + `MORTGAGE_PRINCIPAL_NOT_DEDUCTIBLE` question; excluded from `total_operating_expenses`, visible in `separated_not_expenses` | `test_mortgage_principal_is_separated_and_questioned` |
| Escrow double counting | equal expense/escrow amounts → `ESCROW_DOUBLE_COUNT` ("enter the figure once"); 1098 box 1 cross-checked, mismatch → `MORTGAGE_INTEREST_MISMATCH` | `test_escrow_double_counting_is_flagged`, `test_mortgage_interest_mismatch_is_flagged` |
| Possible improvement | `CAPITAL_IMPROVEMENT_SUSPECT` carries the recorded authority citation; "this audit does not decide" | `test_capital_improvement_suspect_surfaces_authority` |
| Deposits | separated + `DEPOSIT_TREATMENT` ("held in trust…") | `test_security_deposits_are_separated_and_questioned` |
| Mixed use | day counts preserved and **never applied**; `MIXED_USE_ALLOCATION` question | `test_mixed_use_preserves_day_counts_and_questions_allocation` (also `PARTIAL_OWNERSHIP` preserved, not scaled) |
| Missing prior asset schedule | `MISSING_ASSET_SCHEDULE` ("not inferred… not zero"); zero prior depreciation on an older asset → `SUSPECT_MISSING_DEPRECIATION_HISTORY`; recorded history renders as context | `test_missing_asset_schedule_is_a_question_not_zero`, `test_zero_prior_depreciation_for_older_asset_is_suspect`, `test_recorded_depreciation_appears_as_context` |
| Corrected statement | superseded expenses excluded (service cascade covers `RentalExpenseRecord`), correction note states the lineage; missing replacement evidence leaves the category at zero | `test_corrected_statement_excludes_superseded_expenses` |
| Unresolved prevents fully checked | any question, or an out-of-scope property, blocks `fully_checked` | `test_unresolved_questions_prevent_fully_checked`, `test_out_of_scope_property_cannot_be_fully_checked` |
| Suspended losses | `MISSING_SUSPENDED_LOSS_HISTORY` when prior year is unknown | `test_suspended_loss_history_missing_is_a_question` |
| Comparisons | all five statuses with exact deltas and member traceability | `test_comparisons_matched_discrepant_not_recorded`, `test_comparison_flags_drift_and_orphans`, `test_source_drift_comparison` |
| Amounts trace to documents | every expense/income line carries a document+page+box citation | asserted throughout |
| Bank flows never supply figures | rent-sized deposits/withdrawals move nothing; module structurally imports no ledger/classification symbols | `test_bank_flows_never_supply_rental_figures`, `test_module_reads_no_bank_flow_tables` (AST) |

## Design decisions

- **Read-only**, like TAX-13: questions are returned as data; recording or
  replacing TaxAct entries stays with the reviewer via
  `TaxWorkspaceService.record_reconciliation_item` (which retains stale
  history). Deductibility is never decided here — that is the classification
  layer's job, and the consistency test proves the two layers agree: with
  scope configured and treatments classified (with required authority
  citations), reconciliation's deductible bucket equals the paper's
  operating expenses and its non-deductible bucket equals the paper's
  separated lines (`test_consistency_with_reconciliation_rental_totals`).
- **Structural separation instead of judgement**: mortgage principal and
  security deposits are the only categories excluded from the operating
  total, because they are structurally not Schedule E line 12 expenses (the
  fixture's own authority note says so); everything else is presented as
  reviewed and questioned where suspect.
- **Ownership/use facts preserved, never applied**: basis points and
  rented/personal days ride on the paper; partial ownership or mixed use
  raises a question instead of silently scaling amounts.
- **Income attribution** via the property's statement documents (documents
  its expense records reference, including superseded ones for lineage
  notes). This matches how the rental statement links to a property in
  practice without inventing a schema link.
- **Comparison keys**: `"{property_id}.rents"`, `"{property_id}.expenses.{category}"`,
  `"{property_id}.total_operating_expenses"` under section "Schedule E" —
  property-scoped so TAX-08 can bind every required TaxAct field per
  property. Statuses are plain strings (the StrEnum identity trap, again).
- ComparisonRow is defined locally rather than imported from
  `tax/investments.py`: the audit tracks are parallel, and coupling them
  would make TAX-13/TAX-14 unpickable separately.

## Handed to the coordinator

1. **One navigation line** in `app.py` when the audit-wave pages merge
   (page complete and boot-tested):
   `st.Page("app_pages/tax_rentals.py", title="Tax Rentals", icon=":material/home_work:")`
2. At the time of writing, the TAX-15 agent's earlier failures were fixed
   in-tree; the full suite is green with this work included.

## Verification at hand-off

- My files: `ruff check` clean.
- Full default suite: **605 passed, 10 skipped** (was 581 passed + 2
  in-flight failures before this track and the TAX-15 fixes; my additions:
  22 tests — 21 service + 1 page boot).
- No personal records used; all evidence synthetic, created through the
  real service APIs (`create_rental_property`, `record_rental_expense` /
  `verify_rental_expense`, `propose_field` / `verify_field`,
  `register_document` with correction supersession,
  `record_reconciliation_item`, `WorkspaceScopeService.configure`,
  `TaxClassificationService.classify` with authority citations).
