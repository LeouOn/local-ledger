# TAX-06 work log — hardening pass

Completed September 29, 2026. Status: **implemented; hardening acceptance met**.

Implements the resolved design in
[tax-06-hardening-proposal.md](tax-06-hardening-proposal.md), in response to the
P0 findings in [review-2026-09-29.md](review-2026-09-29.md).

Unit suite: **515 passed, 10 skipped** (was 486). Lint clean. Alembic head
`e2b47a9c6d10`.

## What changed

| File | Change |
| --- | --- |
| `tax/classification.py` | Decisions are versioned, source-bound and replay-safe; `classify()` no longer accepts an amount |
| `tax/scope.py` (new) | Persisted, versioned review scope with three modes |
| `tax/reconciliation.py` | Scope-driven completeness, current-revision-only readers, source-change detection, distinct investment measures |
| `tax/service.py` | `_invalidate_downstream` now includes runs and does not commit; `invalidate_evidence` is the public entry point |
| `alembic/versions/e2b47a9c6d10` | `reviewscope`, `decisionhead`, five classification columns, four run columns, legacy backfill |
| `ledger/invalidation.py` | Already covered ledger edits; now consistent with the single path |

## The six P0 findings, each verified fixed

Reproduced first, then re-run after the change. All six now behave:

| Finding | Before | After |
| --- | --- | --- |
| Repeated classification | 12,345c expense → **24,690c** total, two rows | **12,345c**, one current revision |
| Failed split | prior decision erased, half-applied row computing **0** | refused; prior decision intact, allocation **-100,000c** |
| Unverified source in a deductible total | PROPOSED 50,000c expense counted | classification **refused**; not in any total |
| Unclassified transaction | `unresolved_count=0`, invisible | `unresolved_count=1`, 50,000c reported |
| Corrected document | run stayed `CURRENT` | run **STALE**, fresh compute 0 |
| Locked workspace | `classify()` succeeded | `WorkspaceLockedError` |

## Decisions implemented as resolved

**A — replay-safe history.** One `DecisionHead` per (workspace, subject, measure)
owns the current revision. An identical submission returns that revision with no
write; a changed one supersedes the previous rows and keeps them readable. The
comparison covers the whole decision — every part's treatment, fact, authority,
share and property — plus the source fingerprint, with whitespace normalised and
no semantic similarity. Comparison is against the **current** revision only, so an
old decision cannot be resurrected.

**B — the workspace owns versioned scope.** `reviewscope` holds mode, accounts,
properties and a version. `UNCONFIGURED` cannot yield a complete run;
`DOCUMENTS_ONLY` is a real mode; `SELECTED_ACCOUNTS` refuses to be an empty
fallback unless the user confirms no properties apply. Changing scope bumps the
version and stales runs, items and packets in the same transaction. A run stores
its scope version and snapshot, so it can still explain itself after the scope
moves.

## Invariants added beyond the report

- **Amount authority.** The amount is read from the source row, never accepted
  from the caller, and the reviewed fingerprint is stored on the decision.
- **Source change requires review.** A changed source moves the decision to
  `NEEDS_FACT` and it stops counting. Readers refuse a mismatched decision even
  before `revalidate()` records it, so a caller who forgets to run it still
  cannot publish an approval for a new amount.
- **Investment measures are distinct.** `PROCEEDS`, `BASIS`, `ADJUSTMENT` and
  `GAIN_LOSS` are separate measures on separate heads. Proceeds cannot supply
  basis, and a lot with no reported basis stays unknown — classifying it raises.
- **Current-revision-only arithmetic.** Readers resolve through the head;
  `for_subject()` keeps returning history for display but is no longer used for
  totals.
- **Incomplete allocations are explicit.** Every part of a split must total
  10000bp; a partial set is an error, never a fallback zero.
- **One invalidation path, no self-commit.** `_invalidate_downstream` adds and
  flushes only, so an invalidation lands in the caller's transaction.
  Classification writes, scope changes, field verification, document
  corrections and ledger deletions all route through it.

## Migration behaviour

`e2b47a9c6d10` preserves legacy rows and never silently approves them. A legacy
group with exactly one live row becomes a complete revision 1. A group with
several live rows cannot be shown to total 10000bp, so every member moves to
`NEEDS_FACT` with a recorded reason and no head is created — it must be
re-reviewed. Round-trips cleanly and leaves existing workspaces `UNCONFIGURED`.

## Two defects I introduced and fixed mid-pass

1. **A circular import.** `classification.py` imported `Transaction` from
   `local_ledger/models.py`, which imports the tax models to register them. It
   worked only because `models` happened to be imported first; importing
   `tax.classification` first raised. Now imported lazily inside the function
   that needs it, and both import orders are tested.
2. **`"SELECTED_ACCOUNTS" is ReviewScopeMode.SELECTED_ACCOUNTS` is False.** A
   StrEnum member loaded from the database is a plain string, so my identity
   comparisons silently took the wrong branch and every scope-driven total came
   back empty. All comparisons on persisted enum values now use `==`. This is the
   same trap that bit `EvidenceStatus` earlier; it is worth a lint rule.

## Verification

Every fix was verified by reverting it and confirming the matching test fails:

| Reverted | Tests that fail |
| --- | --- |
| Stale-request compare-and-set | 2 |
| Replay idempotency | 2 |
| Decision-change invalidation | 1 (found by this check — no test covered it; one was added) |
| Whole-document PDF fallback (earlier today) | 2 |

Concurrency is tested against a real file-backed database with independent
sessions: a stale reviewer gets a conflict and cannot overwrite a newer review;
the unique head constraint rejects a second head; an identical retry returns the
same revision without bumping the head; and failure injection mid-write restores
the previous revision, its state and its head version.

## Still outstanding (not this pass)

- **Packet readiness** is still the open-question gate from the TAX-04 hardening.
  The proposal asks for scope/revision/pending-evidence checks in the same
  operation; that is a TAX-08-boundary change and is deliberately not mixed in.
- **The review's P1 items**: original-document viewing beside the field under
  review, the incoherent "Verified Total" KPI, interruptible PDF extraction
  (handed to another agent via `PROMPT-pdf-worker.md`).
- **No retention policy** for superseded history, which now accumulates by
  design.
- **Split allocation is generic for transactions and rental amounts only**;
  investment sales keep separate measure fields until TAX-13 defines allocation
  semantics.


## Coordinator integration follow-up — 2026-09-29

The isolated hardening was followed by real cached-session, simultaneous-write,
scope, import and packet readiness integration checks. Five remaining findings
and two additional stale-read cases were fixed; no new migration was needed.
Full default suite: **557 passed, 10 skipped, 4 deselected**; Ruff and diff checks
clean. See [the integration-fix handoff](tax-06-integration-fixes-2026-09-29.md)
for exact behavior, regression evidence and remaining TAX-08 coverage limits.
