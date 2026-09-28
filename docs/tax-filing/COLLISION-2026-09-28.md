# COLLISION — two agents, one working tree, September 28, 2026

**Read this before touching any tax document.** This is a coordination incident,
not a design decision.

## What happened

Two agents were working in the **same working tree** at the same time, with no
branch or worktree isolation:

- **Agent A** — TAX-01 (import integrity), completed 13:50.
- **Agent B** — TAX-02, then **TAX-03** (contract), then started **TAX-04** at
  13:59.
- **A second agent (this one)** — asked at 13:44 to find work "not currently
  being worked on", read the `tasks.md` status column, correctly saw TAX-03 as
  `Not started`, and took TAX-03.

By the time the second agent actually wrote the files at 14:01–14:03, agent B had
**already completed TAX-03** (13:52) and moved to **TAX-04** (13:59). The status
read was 17 minutes stale, and `docs/tax-filing/` had no claim marker that would
have surfaced the change.

## Damage

**Destroyed (untracked, therefore not recoverable from git):**

| File | Owner | State |
| --- | --- | --- |
| `docs/tax-filing/contracts.md` | agent B (TAX-03) | **Overwritten, lost.** ~13:50 draft, the contract TAX-04 was being built against |
| `docs/tax-filing/tax-03-work-log.md` | agent B (TAX-03) | **Overwritten, lost** |

Checked for recovery: no git object (the directory was untracked), no editor
backup, no temp/trash copy, and the dangling commits in `git fsck` are unrelated
old stashes. There is no copy.

**Intact and unaffected:**

- `docs/tax-filing/fixtures/` — agent B's three JSON fixtures.
- `docs/tax-filing/tasks.md` — the second agent's TAX-03 row edit failed
  atomically, so agent B's status rows were not clobbered.
- `src/local_ledger/tax/{__init__,models,service,storage}.py` — agent B's
  in-flight TAX-04 code, written 14:02–14:04, untouched.
- Agent A's TAX-01 work (import, dedup, models, migration `e5f7a1c9d204`) —
  agent B's TAX-04 migration is being rebased onto it, as planned.

**Mitigation already applied:** the second agent's three documents were moved to
`docs/tax-filing/agentC-drafts/`, each carrying a banner marking it a draft that
must not be built on. The canonical `contracts.md` path is now free for agent B
to restore.

## What this means for TAX-04

The **interface is not lost** — agent B's contract is fully instantiated in their
own code, which is intact. The lost document is the prose: the rationale, the
invariants, the scope confirmations, and the reasoning behind the choices. That
is recoverable only by agent B.

Reviewing their code against the contract that should exist, these are the
places where their schema encodes a decision that deserves an explicit ruling.
**These are questions for agent B and the coordinator, not instructions to
change code mid-migration:**

1. **Unknown vs zero.** `ExtractedField.amount_cents` defaults to `0`,
   `InvestmentSaleRecord.cost_basis_cents` is non-nullable, and
   `JurisdictionCarryover.prior_year_cents` defaults to `0`. The user has **no
   prior-year (2024) documents**, so "no carryover" and "missing cost basis" are
   the normal case, not an edge case. As written, a missing basis or a missing
   carryover silently becomes `0`, which is the one representation guaranteed to
   produce a wrong return. An explicit unknown representation is worth deciding
   before the migration is finalised.
2. **A float in a money schema.** `AssetDepreciation.recovery_years: float`. The
   rest of the codebase is strict integer cents; a float in a tax schema is
   inconsistent, and depreciation is the place rounding quietly accumulates.
3. **Versioning granularity.** `TaxDocument` rows plus `supersedes_doc_id` model
   a correction as a new document. That works, but it means the same bytes under
   a new filename are a *new* document rather than a *new version* of one - worth
   confirming that is intended, because it decides what gets superseded when a
   corrected statement arrives.
4. **Jurisdiction shape.** `TaxWorkspace.jurisdiction` is singular with a unique
   constraint on `(tax_year, jurisdiction)`, so 2025 federal and 2025 California
   are two workspaces. That is a legitimate shape, but it means every
   cross-jurisdiction read has to join two workspaces, and it interacts with
   workspace-scoped uniqueness everywhere.

The second agent's draft answers (1) and (2) differently, and adds a
`taxdocumentversion` table for (3) - but that draft is not authoritative and its
table inventory does not match what agent B built.

## The process fix this exposes

`tasks.md` says agents must "Work in isolated branches/worktrees". That was not
done; all agents shared `/home/yl/proj/local-ledger`, so:

- two agents took the same task from a status column that went stale in minutes;
- one agent's file writes can silently destroy another's untracked work;
- concurrent edits to `tasks.md` can lose updates.

Until that is fixed, any agent picking up a task must re-read the claim
immediately before writing, and `docs/tax-filing/` should be committed (or each
agent should use a worktree) so untracked work is at least recoverable.
