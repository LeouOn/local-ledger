# TAX-03 work log — tax workspace contract and supported scope

Agent B, started September 28, 2026. Status: **completed**.

This log records the goals, scope, data models, state machine, and synthetic fixture
specifications for the tax preparation workspace contract. Acceptance criteria are
documented in [tasks.md](tasks.md#tax-03--define-tax-workspace-contract-and-supported-scope).

## Goals

1. **Formalize the 2025 Federal / California TaxAct review contract.** Define DTOs,
   field representations, integer-cents / fixed-decimal conversion, year isolation,
   and source tracking without touching existing application models.
2. **Strict separation of budget category and tax treatment.** Ensure that budget
   categories are never treated as tax classifications or deductibility determinations.
3. **Evidence lifecycle and supersession.** Specify state machines (`PROPOSED`, `VERIFIED`,
   `REJECTED`, `SUPERSEDED`) and automatic staleness/invalidation semantics when documents
   or source transactions change.
4. **Multi-jurisdiction and carryovers.** Define federal vs. California tracking for basis,
   depreciation, passive activity loss limitations, and capital loss carryovers.
5. **Synthetic fixture suite.** Provide concrete, deterministic synthetic fixtures for
   consolidated brokerage 1099s (INT/DIV/B), rental property statements, and state differences.
6. **Local privacy boundary.** Ensure all tax data structures are explicitly tagged and
   restricted to local-only persistence and evaluation, with zero cloud egress.

## Deliverables

- `docs/tax-filing/contracts.md`: Authoritative data contracts, service interfaces,
  state transitions, and conversion policies:
  - Exact integer-cents & decimal conversion policies.
  - Core DTOs (`TaxDocumentDTO`, `SourceLocator`, `ExtractedFieldDTO`, `InvestmentSaleRecordDTO`,
    `TaxPropertyDTO`, `RentalExpenseRecordDTO`, `AssetDepreciationDTO`, `JurisdictionCarryoverDTO`,
    `ReconciliationItemDTO`, `EvidencePacketManifestDTO`).
  - Evidence lifecycle states: `PROPOSED` -> `VERIFIED` -> `SUPERSEDED` / `REJECTED`.
  - Invalidation rules: document supersession, source transaction edits, and discrepancy flagging.
  - Service protocol interfaces for `TaxWorkspaceService` and `TaxReconciliationService`.
- `docs/tax-filing/fixtures/`:
  - `brokerage-1099-consolidated.json`: Validated synthetic 1099-INT, 1099-DIV, and 1099-B sales
    covering covered/noncovered lots, wash sale disallowance, and missing cost basis.
  - `rental-statement-property-a.json`: Single-family rental operating statement with gross rents,
    categorized expenses, Form 1098 interest vs. principal separation, escrow checks, and capital
    improvement flag.
  - `california-carryovers.json`: Capital loss carryover limits, California non-conforming bonus
    depreciation adjustments, and passive activity loss phaseouts.
- `docs/tax-filing/tasks.md`: Progress tracking and dependency updates.

## Acceptance verification

- [x] One documented contract defines cents/decimal conversion, field types, IDs, year isolation,
      state transitions, corrections/supersession, source hashes, model provenance, and export versioning.
- [x] Applicable-year authority requirement defined for tax classifications.
- [x] Budget category is explicitly detached from tax treatment.
- [x] Strict local containment documented (no raw tax documents in cloud payloads).
- [x] Zero application schema edits or migrations added (avoids competing heads with TAX-01).
- [x] Synthetic fixtures validated as standard JSON and ready for TAX-04/TAX-10 tests.

