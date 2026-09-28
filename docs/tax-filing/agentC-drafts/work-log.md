# TAX-03 work log — tax workspace contract and supported scope

Agent C, completed September 28, 2026. Status: **complete**.

## Deliverables

- [contracts.md](contracts.md) - the single contract: confirmed scope,
  invariants, money/field/ID rules, state machines, corrections and staleness,
  source hashes, model provenance, export versioning, DTO reference, service
  boundaries, TAX-04's table inventory, the authority list, and the privacy
  boundary.
- [fixtures.md](fixtures.md) - the synthetic fixture specification every tax
  agent asserts against, with exact cents and an `expected.json` block.

No application schema was changed, as scoped. No migration was created.

## Scope confirmed with the user

2025, federal + California, **TaxAct Online**, investments and rental income,
**full-year California resident**, all rentals in California with **entity or
complex ownership**, investment scope = 1099-INT/DIV/B + stock/fund sales with
basis + **digital assets (1099-DA)**, K-1s excluded, and **no prior-year (2024)
documents**. Recorded in contracts.md §1.

## Two scope findings the confirmation produced

1. **OPEN-1 is a real conflict, not a detail.** Entity-held rentals commonly
   deliver income as a K-1, but K-1s are out of scope. The contract makes K-1 an
   explicit exclusion and gives it a fixture (`oak_2025_k1.pdf`) whose required
   behaviour is an explicit *unsupported* item, not a silent pass. Resolving
   OPEN-1 (with OPEN-2, the ownership structure) flips that fixture's
   expectation.
2. **No prior-year documents makes every carryover `UNKNOWN`.** This is now the
   fixture family's defining constraint rather than a footnote: a missing
   carryover is never zero, and no packet may present a 2025 figure as complete
   while they are unknown (INV-2, INV-11).

## Design decisions worth reviewing

- **Unknown is a first-class value**, not a nullable afterthought: `NULL` plus
  `unknown_reason` everywhere an amount can be absent, because a missing basis
  silently becoming `0` is the failure mode that produces a wrong return.
- **Federal and California are structurally separate rows** keyed by
  jurisdiction, so a "copy the federal figure" shortcut cannot exist.
- **Budget category and tax treatment are separate vocabularies** with separate
  lifecycles; no implicit mapping exists, so a category name can never become a
  treatment (contracts.md §13).
- **Arithmetic never touches a model.** Reconciliation and comparisons are pure
  code; the model may only propose a value with a source locator (INV-8, INV-13).
- **Staleness is computed, not guessed**: totals carry the member ids they
  consumed, so an edit to one of those members invalidates the run (contracts
  §10).
- **The cloud boundary is structural**, not documentary: a required test asserts
  that no tax module imports `get_cloud_client`, `send_chat_request` or
  `build_cloud_payload`.

## Handoff

- **TAX-04** implements contracts.md §14 DTOs and §16 tables verbatim, on
  migration head `e5f7a1c9d204`. It needs no further decisions from TAX-03.
- **TAX-05 through TAX-15** code against the §15 service boundaries and assert
  against `fixtures.md`. A field or table not in §16 is requested from the
  schema owner, not added by a second migration.
- **Coordinator:** contracts.md is the artifact the remaining agents build
  against. Deviations must be recorded in it, not in code comments.
