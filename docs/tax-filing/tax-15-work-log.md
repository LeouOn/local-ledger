# TAX-15 work log — Track federal/California differences and prior-year carryovers

Agent C, completed October 1, 2026. Status: **COMPLETE**.

This log records the implementation of federal vs. California tax differences, residency and property location sourcing facts, separate asset depreciation schedules (FTB 3885A vs Form 4562), prior-year carryovers (Schedule D capital losses, FTB 3801 passive rental losses), Schedule CA (540) adjustment synthesis, and TaxAct reconciliation items generation.
Acceptance criteria are in [tasks.md](tasks.md#tax-15--track-federalcalifornia-differences-and-prior-year-carryovers).

## Goals & Outcomes

1. **Official 2025 California Tax Authority Registry (`src/local_ledger/tax/california.py`):**
   - Implemented `CaliforniaTaxAuthority` and seeded `OFFICIAL_2025_CA_AUTHORITIES` with official statutory authorities:
     - `BONUS_DEPRECIATION`: R&TC §§ 17201 & 17250 / IRC § 168(k), FTB Form 3885A vs Form 4562, Schedule CA Line 8f. California does not conform to federal bonus depreciation.
     - `CAPITAL_LOSS_CARRYOVER`: R&TC § 18151 / IRC §§ 1211 & 1212, FTB 540 Schedule D vs Form 1040 Schedule D, Schedule CA Line 7. Conforms to $3,000 ordinary income offset limit, but capital gains are taxed as ordinary income in CA.
     - `PASSIVE_RENTAL_LOSS`: R&TC § 17551 / IRC § 469, FTB 3801 vs Form 8582, Schedule CA Line 8c. Tracks suspended passive rental losses with CA-specific MAGI phaseouts.
     - `RESIDENTIAL_RENTAL_DEPRECIATION`: R&TC § 17250 / IRC § 168(c), 27.5-year straight line conformity for residential rental structures.
     - `SECTION_179_EXPENSE`: R&TC § 17255 / IRC § 179, $25,000 CA limit with $200,000 phaseout threshold (vs >$1M federal).
     - `MUNICIPAL_BOND_INTEREST`: R&TC § 17143 / Cal. Const. art. XIII § 26, Schedule CA Line 2b. Out-of-state muni bond interest is exempt federally but taxable in CA.
     - `HSA_NONCONFORMITY`: R&TC § 17215 / IRC § 223, Schedule CA Line 13. CA does not conform to HSA deduction; contributions and earnings are taxable.
     - `US_OBLIGATION_INTEREST`: 31 U.S.C. § 3124 / R&TC § 17131, Schedule CA Line 2b. U.S. Treasury interest is federally taxable but exempt in CA (Column B subtraction).

2. **Residency Status & Real Property Location Sourcing:**
   - Implemented `ResidencySourcingAnalysis` and `analyze_residency_and_sourcing(...)`:
     - `ResidencyStatus.UNKNOWN`: Honest default; raises blocking review questions and explicitly forbids marking California treatment as verified.
     - `ResidencyStatus.FULL_YEAR`: Worldwide taxation under R&TC § 17041. All rental properties (in-state and out-of-state) are included in the CA tax base (with Schedule S other state credit potential).
     - `ResidencyStatus.NON_RESIDENT`: California-source taxation only under R&TC § 17951. In-state rental properties are 100% CA-sourced; out-of-state rental properties are 0% CA-sourced and excluded from CA taxable income.
     - `ResidencyStatus.PART_YEAR`: Requires dual-status allocation between resident and nonresident periods under Form 540NR. Flags missing residency dates as a blocker to verification.

3. **Separate Federal vs. California Asset Depreciation Tracking (FTB 3885A vs Form 4562):**
   - Implemented `record_depreciable_asset(...)` and `analyze_depreciation_differences(...)`:
     - Tracks cost basis, non-depreciable land value, recovery periods, prior depreciation (Fed vs CA), and current-year depreciation (Fed vs CA).
     - Evaluates current-year depreciation delta:
       - CA > Fed: Schedule CA (540) Column B subtraction (higher state expense deduction).
       - Fed > CA: Schedule CA (540) Column C addition (higher federal deduction).
       - Fed == CA: No difference (e.g. conforming 27.5-year residential structures).
     - Tracks cumulative remaining basis for both federal and California to audit gain/loss upon asset disposition.

4. **Prior-Year Carryovers Tracker (Schedule D / FTB 3801):**
   - Implemented `record_carryover(...)` and `analyze_carryover_differences(...)`:
     - Handles `CAPITAL_LOSS` and `PASSIVE_ACTIVITY_LOSS` categories across `US-FED` and `US-CA` jurisdictions.
     - **Critical Invariant:** Missing prior-year returns (`prior_year_cents = None`) are **never** silently coerced to $0. They are marked `MISSING_PRIOR_RETURN` and raise blocking verification gaps.
     - Tracks current-year utilization and remaining carryovers.

5. **Schedule CA (540) Adjustments Synthesis & TaxAct Comparison Items:**
   - Implemented `compute_schedule_ca_adjustments(...)`:
     - Synthesizes depreciation differences, carryover deltas, and residency blockers into a structured `ScheduleCAAdjustmentReport`.
     - Calculates total Column B subtractions, total Column C additions, and net CA adjustment.
     - Readiness gate: reports `is_ready_for_review=False` if any unresolved questions or unconfirmed residency remain.
   - Implemented `generate_taxact_reconciliation_items(...)`:
     - Generates audited `ReconciliationItem` records mapped to `California > Schedule CA (540)`.
     - Assigns statuses (`MATCHED`, `DISCREPANCY`, `UNRESOLVED`) based on TaxAct input comparison and workspace readiness.

6. **Streamlit UI Page & Navigation:**
   - Implemented `app_pages/tax_california.py`:
     - Workspace selection (year and jurisdiction) and workspace lock protection.
     - KPI cards: Residency Status, Column B Subtractions, Column C Additions, Net CA Adjustment.
     - Tab 1: **Residency & Sourcing**: Status confirmation form and property-by-property sourcing table.
     - Tab 2: **Depreciation Differences**: Asset schedule table, federal vs CA basis/depreciation comparison, and asset entry form.
     - Tab 3: **Prior-Year Carryovers**: Federal vs CA carryover comparison table, missing return warnings, and carryover entry form.
     - Tab 4: **Schedule CA (540)**: Complete Column B & Column C adjustment table, total metrics, unresolved blockers, and TaxAct item generation.
     - Tab 5: **Authority Registry**: Detailed cards with statutory citations, FTB/IRS form pairings, Schedule CA line mappings, and legal explanations.
   - Wired navigation in `app.py`:
     - Added `st.Page("app_pages/tax_california.py", title="California Tax", icon=":material/gavel:")`.

7. **Test Verification & Quality Assurance:**
   - `tests/unit/tax/test_tax_california.py`: 10 comprehensive tests covering authority registry seeding, unknown residency blocker, full-year sourcing, nonresident out-of-state exclusion, part-year dual-allocation requirement, bonus depreciation subtraction, conforming structure zero adjustment, capital loss carryover limits, missing prior return non-zero invariant, and Schedule CA compilation / TaxAct reconciliation item generation.
   - `tests/unit/test_ui_smoke.py`: Added `test_tax_california_page_renders(apptest)` verifying hermetic child process page rendering without errors.
   - Linting: Clean `ruff check` on all modified and newly created files.
   - Suite regression check: Full unit test suite passed: **583 passed, 10 skipped**.

## Acceptance Criteria Verification

- [x] **Differing federal/California basis/depreciation tracked:** Tested bonus depreciation nonconformity with Column B subtractions, remaining basis variance upon disposition, and conforming residential structure zero adjustment.
- [x] **Prior capital loss and suspended rental loss tracked:** Form 1040 Schedule D vs FTB 540 Schedule D ($3,000 limit) and Form 8582 vs FTB 3801 tracked with form line citations.
- [x] **Missing prior return preserves unknowns:** Missing prior-year return (`prior_year_cents = None`) is never coerced to $0 and flags blocking review questions.
- [x] **Part-year / unknown residency flags unknowns:** Unknown residency blocks California verification; part-year resident flags Form 540NR dual-allocation requirement.
- [x] **Real property sourcing rules enforced:** California real property is 100% CA source; out-of-state property is 0% CA source for nonresidents.
- [x] **Input and comparison checks without guessing tax owed:** Checks concern documented inputs, Schedule CA adjustments, and TaxAct comparisons, with zero model arithmetic and strict integer cents.
