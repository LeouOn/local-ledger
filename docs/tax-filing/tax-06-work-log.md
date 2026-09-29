# TAX-06 work log — annual reconciliation and tax classification

Completed September 29, 2026. Status: **implemented**.

## What this delivers

Two services that every later audit track depends on, and the first code to
read the synthetic fixtures in `docs/tax-filing/fixtures/`.

- **`tax/classification.py`** — the registered treatment vocabulary and the
  record of the fact and authority behind each treatment.
- **`tax/reconciliation.py`** — exact, reproducible, traceable tax-year totals
  per basis (source-form vs bank-flow), plus persisted runs with member ids.

## The four decisions that shape the design

**1. Source-form totals and bank-flow totals are different numbers and are
never added together.** What a 1099 says and what the bank moved are separate
evidences. A brokerage transfer is a real cash movement and no income at all.
`compute_run` therefore produces one run per `basis`, and income and excluded
flows get separate field keys (`income:TRANSFER_OUT` vs
`excluded:TRANSFER_OUT`) so a caller cannot add a transfer into an income total
by accident.

**2. A total carries the ids of the rows that produced it.** Every `Total`
holds `member_ids` and `excluded_ids`, and a persisted run hashes the totals
*together with their members*. That makes a run reproducible (same evidence ->
same hash), makes a changed run detectable without diffing numbers, and means
any figure can be taken apart to the evidence beneath it.

**3. Unknown is not zero — in either direction.** Unverified and undecided
amounts are counted in `unresolved_cents` and excluded from totals rather than
folded in. The brokerage fixture's noncovered lot is the case that matters: its
basis is `None`, so it is counted as unresolved instead of contributing a zero
that would make the basis total look complete. A rental expense that matches no
registered treatment is likewise `unresolved`, never guessed.

**4. Only transfers are decided by code.** A paired transfer row is a structural
fact, so it is determined automatically. Everything else — is this a repair, is
this refundable, does this belong to the rental — is a judgement, and is recorded
as a reviewed classification with a `fact` and, for legal-test treatments, an
`authority_citation`. `CATEGORY_HINTS` maps statement labels to treatments as a
*starting point*, and anything unmapped stays unresolved.

## Two things I want flagged

**I added a schema migration, which was outside TAX-06's stated owner scope**
("no shared schema edits"). It was unavoidable: a reviewed classification and a
traceable snapshot had nowhere to live. The new tables are minimal
(`taxclassification`, `reconciliationrun`) and the totals live as canonical JSON
on the run, matching the existing `consumed_transaction_ids_json` convention
rather than adding two more tables that are only ever read together. Migration
`c3d91f6a2b48` sits on top of agent B's `f6a8b2c4d305`. **If the coordinator
prefers this modelled differently, this is the change to revisit** — it is
deliberately isolated in one migration.

**`bank_flow_totals` needs `account_ids`.** It derives them from reconciliation
items that consumed transactions, which is a reasonable default but a weak
inference; TAX-05 or TAX-06's caller should pass accounts explicitly. Left as a
narrow default rather than guessed at.

## Notable implementation detail

`allocate_cents` exists because integer division does not divide evenly, and the
remainder handling differs by sign. Python's `//` rounds towards minus infinity,
so a naive 73/27 split of a negative total quietly *loses* a cent — which for a
tax figure is a total that no longer matches its evidence. Remainder cents go to
the largest shares one each, and the rows of a split are only ever distributed as
a group (`allocations_for`), since a share is meaningless in isolation: giving a
single 73% row the whole remainder would hand it 100%.

## Test evidence

`pytest tests/unit -q` → **449 passed, 10 skipped** (was 420). `ruff check` clean
across `src`, `app_pages`, `tests`.

29 new tests in `tests/unit/tax/test_tax_reconciliation.py`, covering the
acceptance criteria: transfers and refunds excluded from income, unresolved
amounts held out of totals, corrected documents dropping the old figure, a
superseded amount unable to return, splits allocating exactly (including
negative totals), re-splitting not double counting, authority required for legal
tests, unregistered treatments refused, the rental fixture reconciling to its
statement, the capital-improvement suspect staying out of expenses, mortgage
principal tracked as not-an-expense, unreported basis excluded from basis totals,
year isolation, reproducibility and hash stability, ledger edits staling a run,
and the no-model-dependency guard.

Every fix was verified by reverting it and confirming the matching test fails:
dropping the VERIFIED gate (4 fail), treating transfers as income (2 fail),
zeroing unreported basis (1 fail), dropping member ids (1 fail), removing the
run-staleness hook (1 fail), dropping the authority requirement (1 fail), and
allowing a split that does not add up (caught by `allocate_cents`).

## Handoff for the next tracks

- **TAX-13/14/15** call `rental_totals(property_id)`, `investment_totals()` and
  `source_form_totals()` for verified working-paper figures, and record reviewed
  treatments through `TaxClassificationService`. A lot or expense whose treatment
  they cannot settle becomes a question, not a total.
- **TAX-08** compares a user-entered TaxAct amount against a total **of the same
  basis** and records the delta on `ReconciliationItem`.
- **TAX-11** should assert that a run's `content_hash` is stable across
  recomputation and changes when evidence changes — that is the reproducibility
  contract this task established.
- Two scope questions still gate the audit tracks: whether entity-held rental
  income arrives as a K-1 (out of scope, would change the whole input shape), and
  the absence of 2024 documents (every carryover stays `UNKNOWN`).
