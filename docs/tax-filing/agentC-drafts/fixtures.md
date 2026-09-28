> **DRAFT - NOT THE ACCEPTED FIXTURE SET. DO NOT ASSERT ON THIS.**
>
> A second, independent fixture specification written by a different agent after
> agent B had already produced `docs/tax-filing/fixtures/` (three JSON fixtures,
> intact). Its numbers were authored independently and **will not match**
> agent B's fixtures. Having two competing "expected values" sources is the
> exact failure the backlog warns about, so this is parked here for review only.
> The accepted fixtures are in `../fixtures/`.
> See [../COLLISION-2026-09-28.md](../COLLISION-2026-09-28.md).

# Synthetic tax fixture specification (agent C draft)

Created September 28, 2026 by agent C for **TAX-03**, implementing the "synthetic
fixture specification" deliverable. The contract it serves is
[contracts.md](contracts.md); the numbers below are the assertable half of it.

These fixtures are the shared ground for TAX-05 (intake), TAX-06
(reconciliation), TAX-10 (model qualification), TAX-11 (journeys), TAX-13
(investments), TAX-14 (rentals) and TAX-15 (jurisdiction differences). One
fixture set, one set of expected values - so two agents cannot disagree about
what "the" 2025 numbers are.

## 0. Rules for every fixture

1. **Synthetic only.** No real account numbers, SSNs, EINs, addresses, statement
   numbers or values. Use `SYNTH-*` institutions, `***-**-0000`-style masked
   identifiers, and obviously synthetic names.
2. **Exact cents.** Amounts are authored in cents, never dollars-as-decimals, so
   a fixture cannot hide a conversion bug.
3. **One tax year: 2025.** A 2024 document appears only in the negative cases,
   where its purpose is to be rejected.
4. **Nothing derived from user data.** Fixtures are checked in or generated from
   a seeded generator; no test may read `data/ledger.db`.
5. **Expected values are declared, then asserted.** `expected.json` is the
   contract; a test failure means either the code or the fixture drifted, and
   both are bugs to be discussed, never silently re-baselined.
6. **No fixture asserts a tax result.** Expected values are documented *inputs*
   (proceeds, basis where reported, rent, expenses) and the questions that must
   be raised. The app never computes gain, deduction or tax owed (INV-8).

## 1. Layout and manifest

```
tests/fixtures/tax/
  manifest.json              # fixture_set_id, version, registry/manifest versions, file hashes
  expected.json              # every expected total and count, keyed by name
  ledger/
    checking_2025.csv        # F1, importable through the real CSV import path
    brokerage_2025.csv       # F1b, the brokerage cash account
  documents/
    1099b_2025_original.pdf      # F2a  (text layer)
    1099b_2025_corrected.pdf     # F2b  (supersedes F2a)
    1099int_2025.pdf             # F2c
    1099div_2025.pdf             # F2d
    1099da_2025.pdf              # F2e
    rent_oak_2025_statement.pdf  # F3a
    mortgage_oak_2025.pdf        # F3b
    escrow_oak_2025.pdf          # F3c
    rent_pine_2025_statement.pdf # F3d
    oak_2025_k1.pdf              # F3e  (unsupported path, see OPEN-1)
    prior_return_2024.pdf        # F5   (excluded: no prior-year documents)
  scanned/
    1099div_2025_scanned.pdf     # F4a  image only, no text layer
  unsupported/
    statement_2025.xlsx          # F4b
```

`manifest.json` carries `fixture_set_id` (e.g. `tax-fixtures/1`),
`tax_year: 2025`, `registry_version: "tax-registry/1"`,
`manifest_version: "tax-packet/1"`, and a `sha256` per file. Tests that verify
document hashes use the manifest, never a recomputed guess.

### 1.1 Building the PDFs

- `documents/*.pdf` must have a real text layer, with text positioned so page
  numbers are recoverable (fixtures assert `page` values).
- `scanned/1099div_2025_scanned.pdf` must be image-only: a rendered page with
  **no** text objects, so text extraction yields nothing and the intake path
  must classify it as `scanned_no_text`.
- Page count is asserted per document, so "page references were preserved"
  (TAX-05) is testable rather than asserted.

## 2. F1 - Ledger fixtures

Imported through the real CSV path, with `FITID` values, so the TAX-01 import
contract (staging, acceptance, idempotency, provenance) is exercised end to end.

### F1 `ledger/checking_2025.csv`

| FITID | date | payee | amount_cents | meaning for tax |
| --- | --- | --- | --- | --- |
| L-001 | 2025-01-02 | OAK ST HOLDINGS LLC | 3600000 | rental income, entity path (see F3e) |
| L-002 | 2025-01-05 | FIRST NATIONAL MORTGAGE | -1987200 | P&I 1441200 + escrow 546000, blended into one row |
| L-002b | 2025-01-05 | OAK ST ESCROW DISBURSEMENT | -546000 | the **same** property tax debited again - the double-count case |
| L-003 | 2025-03-31 | TRANSFER TO SAVINGS | -5000000 | paired transfer - excluded from income |
| L-004 | 2025-04-10 | TRANSFER FROM BROKERAGE | 25000000 | **not** a capital gain (INV-7 in practice) |
| L-005 | 2025-06-10 | REFUND - HOME DEPOT | 45000 | refund - classified, not income |
| L-006 | 2025-07-04 | THE CORNER BISTRO | -8240 | personal, not a rental expense |
| L-007 | 2025-08-01 | SYNTHETIC CAFE | -450 | equal-valued pair, part 1 |
| L-008 | 2025-08-01 | SYNTHETIC CAFE | -450 | equal-valued pair, part 2 |
| L-009 | 2025-12-15 | OAK ST HOLDINGS LLC | 3600000 | second quarter rent |

Expected: 10 ledger transactions; 2 excluded as transfers (`L-003`, `L-004`);
1 refund classified (`L-005`); 1 personal (`L-006`); the equal-valued pair
`L-007`/`L-008` **both retained** (TAX-01 rule, and a regression guard here).
`L-002` decomposes exactly to P&I + escrow, so a correct split reconciles to
`1987200` and `L-002b` is the duplicate that must be caught.

### F1b `ledger/brokerage_2025.csv`

Cash movements only, deliberately useless as tax figures:

| FITID | date | payee | amount_cents | purpose |
| --- | --- | --- | --- | --- |
| B-001 | 2025-04-10 | TRANSFER IN | 1200000 | matches 1099-B lot A proceeds exactly |
| B-002 | 2025-09-20 | TRANSFER IN | 3500000 | matches the *original* lot B proceeds |
| B-003 | 2025-12-20 | WITHDRAWAL | 500000 | has no 1099-B counterpart at all |

`B-003` is the trap: a cash withdrawal is not a sale. TAX-13 must never derive a
gain from `B-003`, and `B-002` is stale after the corrected 1099-B restates lot B
to 3400000. Expected: 0 sales derived from bank rows; every sale record traces to
a 1099-B source locator.

## 3. F2 - Brokerage documents

### F2a `1099b_2025_original.pdf` - 2 pages, text layer

Payer: `SYNTHETIC SECURITIES LLC`. Payer TIN masked `***-**-0000`.

| Lot | description | acq date | sold date | proceeds_cents | basis_cents | box | term |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | 100 sh SYNTH-CORP | 2022-03-15 | 2025-04-10 | 1200000 | 800000 | D | LONG |
| B | 50 sh SYNTH-ETF | 2025-06-02 | 2025-09-20 | 3500000 | 3500000 | A | SHORT |
| C | 25 sh SYNTH-FUND | **not reported** | 2025-11-05 | 2200000 | **not reported** | A | unknown |
| D | 10 sh SYNTH-CORP | 2025-02-11 | 2025-03-14 | 1000000 | 900000 | A | SHORT |

Form totals printed on the document: proceeds 7900000; box D proceeds 1200000;
box A proceeds 6700000; box D basis 800000; box A basis 4400000. The printed
**basis total of 5200000 excludes lot C, whose basis is marked not reported** -
so the printed basis total is incomplete by construction, and an audit that
reuses it as a complete basis is wrong (INV-2).

Lot C's basis is **not reported**: the expected result is `basis UNKNOWN`, never
`basis = 0` and never a total that pretends to be complete.

### F2b `1099b_2025_corrected.pdf` - supersedes F2a

- Lot B proceeds restated 3500000 -> 3400000.
- New lot E: 30 sh SYNTH-ETF, acq 2023-08-30, sold 2025-12-20, proceeds 700000,
  basis 650000, box D, LONG.
- Printed corrected totals: proceeds 8500000; box A proceeds 6600000, box D
  proceeds 1900000; reported basis 5750000 (still excluding lot C).

After supersession, every reconciliation run, packet and comparison built on
F2a must be `STALE` (INV-10). The original file remains readable and its
recorded lot values remain queryable (INV-9).

### F2c `1099int_2025.pdf`

Interest 14532. Early withdrawal penalty 0. Federal tax withheld 0. Form 1099-INT.

### F2d `1099div_2025.pdf`

Ordinary dividends 241055; qualified dividends 182000; capital gain
distributions 30000; federal tax withheld 0. Form 1099-DIV.

### F2e `1099da_2025.pdf` - digital assets

One disposition: asset `SYNTH-COIN-1`, date disposed 2025-08-20, proceeds
90000, **cost basis not reported**, date acquired not reported, custodian
`SYNTH-CUSTODIAN`. One non-sale receipt of 2500 (a transfer in, not income).

Expected: a sale record with `basis_cents = None` and an open question, plus
one *unsupported-item* for the non-sale receipt. Treatment is authority-gated
([contracts.md §17](contracts.md#17-decisions-that-require-applicable-year-authority));
the fixture asserts the question exists, not an outcome.

## 4. F3 - Rental fixtures

Two California properties, matching the confirmed scope (all in CA, one in an
entity, one individually owned). Neither has prior-year documents.

### F3 property `prop_oak` - "Oak St", entity-held

- CA, `ownership_kind: llc`, share 10000 bp, placed in service 2019-06-15,
  basis 48000000, use allocation 100% residential rental.
- Rent received (from the ledger, `L-001` and `L-009`): 3600000 + 3600000 =
  **7200000** for the year. The documents state the same total; the audit
  compares them rather than adding a third time.

| Expense document | line | amount_cents |
| --- | --- | --- |
| F3b mortgage | annual P&I (principal + interest) | 1441200 |
| F3b mortgage | of which interest | 1390000 |
| F3b mortgage | of which principal | 51200 |
| F3c escrow | property tax paid from escrow | 546000 |
| F3a statement | plumbing repair | 127500 |
| F3a statement | appliance repair | 48000 |
| F3a statement | **roof** | 860000 |

- The **roof** is a possible improvement, not an assumed repair: the expected
  result is an open question with `resolves_by: "repair vs improvement
  determination"`, never a silent classification (INV-14, contracts §17).
- The **escrow property tax 546000 appears twice** - in the mortgage statement
  escrow line (inside `L-002`) and again as its own ledger row `L-002b`. The
  expected result is a double-counting question, and the audit total counts it
  once.
- The ledger row `L-002` (-1987200) is a single blended payment; the audit
  splits it against the statement's P&I and escrow lines and must reconcile to
  exactly 1987200 with no residual.

### F3 property `prop_pine` - "Pine Ave", individually held, mixed use

- CA, `ownership_kind: individual`, share 10000 bp, placed in service
  2021-09-01, basis 31500000, **use allocation 8000 bp rental / 2000 bp
  personal**.

| Document | line | amount_cents |
| --- | --- | --- |
| F3d statement | rent received | 2400000 |
| F3d statement | utilities | 312000 |
| F3d statement | insurance | 186000 |
| F3d statement | management fee | 240000 |

- Documented expenses: 738000; the 8000 bp allocation is exact (no rounding
  ambiguity in this fixture by design - a fixture whose allocation is not
  exactly representable must be a separate case, not a hidden rounding).
- **No prior asset schedule** exists, so prior/2025 depreciation is `UNKNOWN`
  with reason "no prior-year document", never an estimated life (INV-2).

### F3e `oak_2025_k1.pdf` - the OPEN-1 variant, currently unsupported

Form 1065 Schedule K-1 from `OAK ST HOLDINGS LLC` for 2025 showing the same
Oak St rent as entity-level income, instead of direct Schedule E income.

**K-1 is out of scope** ([contracts.md §2](contracts.md#2-explicit-exclusions)).
The required behaviour is an explicit *unsupported / unresolved* item naming
this file, never a silent pass and never a total computed from it. If the user
confirms OPEN-1, this fixture becomes in-scope and the expectation flips to a
supported path with its own totals.

## 5. F4 - Negative and edge cases

| Case | Fixture | Required behaviour |
| --- | --- | --- |
| F4a | scanned 1099-DIV (image only) | `scan_state = scanned_no_text`; manual entry offered; **never** "0 fields extracted" as a success (INV-11) |
| F4b | `statement_2025.xlsx` | `unsupported_type` visible in the inventory; no fields |
| F4c | upload `1099div_2025.pdf` twice | same `sha256` -> one document version, one set of amounts, no double count (contracts §11) |
| F4d | `prior_return_2024.pdf` staged into the 2025 workspace | `workspace_mismatch`; rejected from the 2025 workspace, never folded in |
| F4e | commit the same staged batch twice | one set of transactions (TAX-01) |
| F4f | equal-valued pair `L-007`/`L-008` | both retained (TAX-01), and both excluded from tax totals as personal |
| F4g | corrected 1099-B after a run was computed | that run is `STALE`; the packet cannot be `approved` (INV-10) |
| F4h | 2024 amounts appearing in a 2025 join | impossible by construction; the year-isolation test asserts it (INV-5) |

## 6. F5 - The absent prior year

`prior_return_2024.pdf` is listed in the layout only so the inventory can show a
*missing expected document*. The 2025 workspace has **no** 2024 documents
(confirmed with the user, OPEN-3).

Required result, asserted by TAX-15 and TAX-08:

| Field | Expected |
| --- | --- |
| federal capital-loss carryover | `UNKNOWN` - reason "no prior-year document" |
| California suspended passive loss | `UNKNOWN` - reason "no prior-year document" |
| federal vs California basis difference | `UNKNOWN` - no CA basis evidence exists |
| prior depreciation, both properties | `UNKNOWN` - no prior asset schedule |

A missing carryover is **not** zero, and no packet may present a 2025 figure as
"complete" while these are unknown (INV-2, INV-11).

## 7. Expected values (`expected.json`)

The assertable contract. Keys are namespaced; amounts are integer cents.

```json
{
  "fixture_set_id": "tax-fixtures/1",
  "tax_year": 2025,
  "ledger": {
    "transaction_count": 10,
    "brokerage_cash_rows": 3,
    "excluded_as_transfer": ["L-003", "L-004"],
    "classified_refund": ["L-005"],
    "equal_valued_pair_retained": ["L-007", "L-008"],
    "escrow_duplicate_rows": ["L-002b"],
    "mortgage_split_sums_to_cents": 1987200,
    "sales_derived_from_cash": 0
  },
  "investments": {
    "form_1099b_original": { "proceeds_cents": 7900000, "reported_basis_cents": 5200000 },
    "form_1099b_corrected": { "proceeds_cents": 8500000, "reported_basis_cents": 5750000 },
    "reported_basis_total_cents": 5750000,
    "unknown_basis_lots": 1,
    "lots": 5,
    "interest_cents": 14532,
    "ordinary_dividends_cents": 241055,
    "qualified_dividends_cents": 182000,
    "capital_gain_distributions_cents": 30000,
    "digital_proceeds_cents": 90000,
    "digital_basis_unknown": 1
  },
  "rentals": {
    "prop_oak": {
      "rent_cents": 7200000,
      "mortgage_principal_cents": 51200,
      "mortgage_interest_cents": 1390000,
      "escrow_tax_cents": 546000,
      "escrow_double_counted": true,
      "repairs_cents": 175500,
      "improvement_candidate_cents": 860000,
      "prior_depreciation": "UNKNOWN"
    },
    "prop_pine": {
      "rent_cents": 2400000,
      "expenses_cents": 738000,
      "use_allocation_bp": 8000,
      "prior_depreciation": "UNKNOWN"
    },
    "k1_variant": "UNSUPPORTED"
  },
  "carryovers": {
    "federal_capital_loss": "UNKNOWN",
    "california_suspended_loss": "UNKNOWN",
    "basis_difference": "UNKNOWN"
  },
  "questions_minimum": 7,
  "unsupported_items_minimum": 2
}
```

`questions_minimum` is 8 because one question may legitimately cover both
missing attributes of the same lot. The floor covers these ten obligations:
lot C missing basis, lot C missing acquisition date, the reported wash-sale
adjustment on lot D, digital-asset basis, digital-asset acquisition date, the
Oak St roof, the Oak St escrow double count, the missing prior-year documents,
the K-1 path, and the `B-003` withdrawal with no 1099-B counterpart.

## 8. Rules each consumer must follow

- **TAX-05 (intake):** F4a/F4b/F4c/F4d drive inventory, scan state and duplicate
  handling; page numbers must be recoverable from `documents/*.pdf`.
- **TAX-06 (reconciliation):** totals come from verified field members, source
  form totals and bank-flow totals stay separate, and `L-003`/`L-004` never
  appear in an income total. F1b is the fixture that proves cash is not gain.
- **TAX-10 (qualification):** scores only extraction accuracy and locator
  validity against these documents. The unsupported fields and F4a are scored as
  correct abstentions, not misses.
- **TAX-11 (journeys):** inventory -> verification -> reconciliation -> question
  resolution -> packet -> backup/restore must end with every `expected.json`
  value reproduced exactly, and F4g (changed evidence after export) must flip the
  packet to `STALE`.
- **TAX-13/14/15:** the investment, rental and jurisdiction fixtures above, with
  the differences preserved rather than reconciled away.

No consumer may add a fixture without a matching `expected.json` entry, and
changing an expected value is a scope decision that belongs in this file.
