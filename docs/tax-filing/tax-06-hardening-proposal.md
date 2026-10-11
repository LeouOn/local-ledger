# TAX-06 hardening proposal — classification and reconciliation integrity

Prepared September 29, 2026, in response to the P0 findings in
[review-2026-09-29.md](review-2026-09-29.md). This document is written to be
read cold: it states what was measured, what is proposed, and which decisions
have now been resolved in review. The design below is ready for implementation;
this review changed documentation only.

Most recently measured suite: **486 passed, 10 skipped**, lint clean. That is
the baseline from the September 29 review, not a new test run for these design edits.

---

## 1. Summary

All four P0 findings in the review reproduce, plus a fifth issue the review
noted in passing (classification writes ignore the workspace lock). They are not
six unrelated bugs. They share one root cause:

> The classification layer **trusts the caller and only ever writes.** It never
> reconciles against the source row, never re-reads before replacing, and never
> checks the evidence state it depends on.

So this is proposed as one structural change with four consequences, rather than
six patches. The headline: **a classification should be a statement about a
specific source row, validated against that row, with a defined lifecycle** —
the same discipline the rest of the tax module already follows for extracted
fields.

The two decisions are settled in §5: retain replay-safe decision history and
persist versioned workspace scope. The implementation also needs the invariants
in §3.7; changing status labels alone is insufficient.

## 2. What was measured

Each was reproduced directly against the current code using synthetic records in
temporary storage. These are reproductions, not inferences.

| # | Finding | Observed |
| --- | --- | --- |
| 1 | Repeated classification duplicates an amount | Two identical `classify()` calls on a 12,345c expense produced a **24,690c** deductible total, with two rows persisted for one subject |
| 2 | A failed split loses the prior decision | A split whose second part lacked required authority raised `ClassificationError` — but the original decision had **already been deleted and committed**, one half-applied row survived, and it computed an allocation of **0** |
| 3 | Rental totals bypass source verification | A **PROPOSED**, never-verified 50,000c expense entered a deductible total |
| 4a | Unclassified rows vanish silently | A 50,000c transaction in an explicitly selected account returned `unresolved_count=0` and no warning |
| 4b | A corrected document leaves runs `CURRENT` | After correcting a document holding a verified 1,000c field, a fresh compute returned **0** while the persisted run was still `CURRENT` |
| 5 | Locked workspaces are not enforced | `classify()` succeeded on a `LOCKED` workspace |

## 3. Proposed changes

### 3.1 Amount authority — the foundation for the rest

A classification currently accepts `amount_cents` from the caller. That is the
loose thread running through findings 1, 2 and 3: the layer records a *claim*
about an amount without establishing its source or the version that was reviewed.

**Proposal.** `classify()` resolves the subject inside the workspace, derives the
amount from the source row, and requires the caller's expected source version
(or deterministic fingerprint) to match the source being reviewed:

| Subject type | Source of truth | Amount read |
| --- | --- | --- |
| `RENTAL_EXPENSE` | `RentalExpenseRecord` (same workspace) | `amount_cents` |
| `TRANSACTION` | `Transaction` (must be in the workspace's tax year) | `amount_cents` |
| `INVESTMENT_SALE` | `InvestmentSaleRecord` (same workspace) | Explicit measure: proceeds, basis, reported adjustment or gain/loss; never interchangeable |

The `amount_cents` argument is removed from the public signature. An unknown
subject, or a subject in another workspace, raises rather than creating a row
that points at nothing. Transactions must also belong to a selected account;
properties and document-backed subjects must belong to the selected workspace.

**Investment measure is explicit.** A sale is not a single interchangeable
amount. `INVESTMENT_BASIS` resolves `cost_basis_cents`, never proceeds; absent
basis remains unknown, including when proceeds are known. Reject unsupported
subject/treatment/measure combinations. Generic split allocation in this pass
is for transaction/rental amounts; investment sales retain their separate
measure fields until TAX-13 defines any additional allocation semantics.

**Source amount and reviewed amount differ in purpose.** Read current source
values for arithmetic, but retain the reviewed source fingerprint and amount
snapshot on the decision revision. If the source changes, the old decision
becomes NEEDS_FACT and contributes only to unresolved work until reviewed again.
Automatically applying an old approval to a new amount is not hardening.

### 3.2 Lifecycle and replay (finding 1)

Add `SUPERSEDED` to `ClassificationState`. Its enum values are stored as TEXT,
but revision grouping, current-decision constraints, reviewed source metadata
and persisted scope **do require a migration**. The states become a small,
explicit graph:

```
UNRESOLVED ──review──> CLASSIFIED ──replacement──> SUPERSEDED
NEEDS_FACT ──review──> CLASSIFIED
CLASSIFIED ──source changed──> NEEDS_FACT
```

- **Identical replay is a no-op.** If the current complete CLASSIFIED decision revision and reviewed source
  fingerprint match the submission, return that revision unchanged. Compare all parts for a split, not an individual row.
  This is the double-click case, and it must cost nothing and change nothing.
- **A changed decision supersedes.** The prior row moves to `SUPERSEDED` and a
  complete decision revision is written, in one transaction. History is
  retained; the old judgement stays visible and out of every total.
- **Only rows in the current complete CLASSIFIED revision with eligible current
  evidence contribute.** All allocation readers must filter by that revision;
  the existing `for_subject()` reads history too and cannot be reused unchanged.
- A source change moves the current decision to NEEDS_FACT; a new review
  supersedes it. Every active allocation set must total exactly 10000 basis
  points. A one-part `classify()` means a complete 100% decision; partial shares
  are accepted only by the atomic split operation.

This matches the `ExtractedField` lifecycle already in the codebase, and matches
the review's instruction to keep rejected/superseded history separate from
pending work.

### 3.3 Split atomicity (finding 2)

**Proposal.** `split()` becomes validate-then-apply inside one transaction:

1. Validate the subject exists and resolve its true amount (§3.1).
2. Validate **every** part first — treatment registered, fact present, authority
   present where required, shares summing to exactly 10000 — with no writes at
   all. Any failure raises and nothing has changed.
3. Only then supersede the prior rows and insert the replacements, and commit
   once.

Consequences: a failed split leaves the previous decision completely intact; no
partial row can survive; incomplete allocations produce an explicit integrity
error or unresolved result, never a fallback zero. Arithmetic uses the current decision group only.
Strict integer validation rejects floats/bools for cents and shares; rounding
retains exact sum conservation for positive and negative source amounts.

### 3.4 Verification gating and locking (finding 3, finding 5)

**Proposal.**

- `rental_totals()` includes an expense only if the underlying
  `RentalExpenseRecord` is `VERIFIED` **and** not superseded. A classification
  over unverified or stale evidence is counted as unresolved, with a reason —
  so it is visible rather than silently dropped. Superseded/rejected historical
  evidence is excluded history, not perpetual unresolved work. Only current
  expected evidence or its replacement is counted toward completeness.
- Two distinct facts stay distinct, as the review asks: **the source amount was
  verified** (TAX-04's job) versus **the tax treatment was reviewed** (this
  layer's job). Totals require both.
- Every write path checks the workspace lock and raises `WorkspaceLockedError`,
  matching `TaxWorkspaceService`. The check lives in one place so a new method
  cannot forget it.

### 3.5 Completeness (finding 4a)

**Proposal.** Reconciliation stops iterating classifications and starts iterating
*the scope*. For the persisted workspace scope (§5), every tax-year transaction is
accounted for as a complete allocation:

- reviewed income → income bucket; reviewed expense → expense bucket;
- reviewed investment proceeds/basis → explicitly named measure buckets,
  with no claim that proceeds are taxable income;
- classified as an excluded flow (transfer, refund, personal) → its excluded
  bucket;
- otherwise → **unresolved**, counted and reported.

The same check applies to rental records: every `RentalExpenseRecord` in scope
for the property is approved, excluded, or unresolved. An unresolved count
counts distinct active subjects, not historical rows or split parts; amounts
are reported within their evidence basis/measure rather than summing unrelated
fields. A total cannot claim completeness while relevant rows are absent.

### 3.6 One invalidation path (finding 4b)

**Proposal.** A single operation, in the tax service, that marks stale together:
`ReconciliationRun`, `ReconciliationItem` and `EvidencePacket` for a workspace,
in the mutation's transaction, and records why. The invalidation operation
flushes/adds changes but does not independently commit; the caller owns rollback.

Called from every event that changes the evidence beneath a result:

| Event | Currently invalidates |
| --- | --- |
| Field verified / rejected | items + packets (via `_invalidate_downstream`) |
| Corrected document registered | items + packets — **not runs** ← the bug |
| Classification written/replaced | **nothing** |
| New document, field proposal or open question | no general downstream invalidation |
| Scope changed | no persisted scope yet |
| Transaction inserted/imported into a selected account | no completeness-run invalidation |
| Ledger row deleted | items + packets (and runs, via a separate path) |

After a ledger edit, re-read the source and compare its fingerprint with the
reviewed decision (§3.1). A changed source requires review; recomputation must
not silently transfer approval. New in-scope transactions also stale prior runs,
even though no old run could have consumed their IDs.

Identify affected workspaces by persisted scope and source relationships, not
only by matched comparison items or existing classification rows. Final packet
readiness checks current scope/revisions, pending evidence, unresolved subjects,
open questions and current comparison results; an open-question count alone
is insufficient. All included snapshot collections must be covered by hashing
and invalidation. Preserve draft exports for incomplete work.

The existing `ledger/invalidation.py` already lazily imports the tax service, so
the ledger→tax call needs no new dependency and no import cycle.

### 3.7 Revision ownership, stale requests and concurrency

Treat each (workspace, subject type, subject ID, measure) as one decision head
pointing at one complete revision. Transactions/rental records use their single
cash/expense measure. Investment proceeds and basis are separate measures;
they must not supersede each other or be combined into a split of proceeds. A revision holds all allocation parts, who reviewed
it, when, the decision content and the reviewed source fingerprint. Preserve
prior parts for history; do not infer their grouping from timestamps.

Use a database-unique head and compare-and-set version update inside the same
transaction as the parts and invalidation. Two sessions must not create two
current decisions. A caller supplies its expected head/source versions; a stale
request fails with a review conflict rather than reverting a newer decision.
An exact retry of the current accepted decision returns it without writes or
invalidation. Persist a request identifier when necessary to distinguish retries
from stale new submissions; its reuse with a different payload is an error.

An invalid input or failed database write must preserve the entire previous
revision and all readiness flags. Status checks alone do not establish these
concurrency guarantees. Use the smallest relational schema satisfying these
invariants; a general event-sourcing framework is unnecessary.

## 4. What deliberately does not change

- **The `ScanState` vocabulary.** Encrypted/malformed documents continue to map
  to `UNSUPPORTED_TYPE` with a reason in the page text, rather than adding enum
  values that downstream switches would need to learn.
- **Totals remain separate by basis.** `SOURCE_FORM` and `BANK_FLOW` are never
  added together, and income and excluded flows keep distinct field keys.
- **Determinism.** No model is involved in any arithmetic. The existing
  `assert_no_model_dependency` guard stays.
- **No tax judgement is automated.** Only transfers are determined by
  structure. Everything else is still a reviewed decision with a recorded fact.
- **Scope of this pass:** TAX-06 integrity only. The review's P1 items —
  original-document viewing in the UI, the incoherent "Verified Total" KPI, and
  interruptible PDF extraction — are a separate track and are not touched here.

## 5. Decisions resolved in review

### Decision A — identical replay is a no-op; changed decisions retain history

**Selected: no-op / supersede, applied to the complete decision revision.**

A changed fact or authority is a new revision, not an additional active amount.
The user can inspect the previous rationale and reviewer. This history serves
the TaxAct input-audit goal directly, so replacing it in place is not worth the
small implementation saving. Cosmetic trimming can be normalized consistently;
do not use fuzzy/semantic similarity to decide that two rationales are equal.

| Option | Benefit | Cost / reason not selected |
| --- | --- | --- |
| **No-op / supersede — selected** | Safe replay; one current allocation; rationale retained | Requires revision grouping and concurrency guard |
| Replace in place | Small API | Loses the previous review explanation unless another audit system is added |
| Reject every repeat | Explicit changes | Turns ordinary retries into errors |

A replay is checked against the current revision and source version, not every
historical row. Returning any matching historical record could conceal a new
source amount or resurrect an old decision. A stale request must not supersede
a newer revision.

### Decision B — the workspace owns versioned review scope

**Selected: persist scope; computation reads it explicitly.**

The user is auditing an annual return across sessions. A stored run or packet
must prove which accounts/properties were considered, and omission of an account
must be distinguishable from having no relevant transactions. Avoiding a small
migration is not a sufficient reason to leave that definition in callers.

Persist selected account/property membership and a scope version for each
workspace. New scope starts **UNCONFIGURED**, which cannot yield a complete
run. Support an explicit **DOCUMENTS_ONLY** mode and a **SELECTED_ACCOUNTS** mode;
selected accounts cannot silently be an empty fallback. Empty property membership
is allowed when deliberately confirmed as not applicable. Validate referenced
accounts/properties and require a selection or explicit exclusion for newly
registered properties before readiness can be asserted.

Changing membership or mode increments the version and atomically stales runs,
items and packets. Runs snapshot the scope version and member IDs. They can
therefore still explain what was reviewed after the workspace scope changes.
Federal/California workspaces may intentionally select the same account; this
never licenses cross-workspace property/document references.

| Option | Benefit | Cost / reason not selected |
| --- | --- | --- |
| **Persisted, versioned scope — selected** | Stable completeness and reproducible reports | Small membership model and migration |
| Call-supplied scope only | Easy isolated computations | Cannot establish the workspace's review boundary across sessions |
| Infer accounts | Least initial churn | Misses unclassified data and cannot prove completeness |

Pure computation may accept an explicit scope snapshot for tests or a clearly
labelled ad-hoc preview. Such a preview cannot become the workspace's approved
run unless the snapshot matches its configured scope version. Never infer scope
from classifications or prior reconciliation items.

## 6. How we will know it worked

Each finding gets a regression test that **fails before the fix**. Reproduced
above, the same reproductions become tests:

| Test | Asserts |
| --- | --- |
| identical `classify()` twice | one current revision, unchanged reviewer/time/total, no invalidation |
| changed `classify()` | prior row `SUPERSEDED`, out of totals, retained |
| `split()` with an invalid part | raises, prior decision fully intact, no partial row |
| `split()` valid | shares total 10000, amounts sum exactly, including negatives |
| source changed / stale review request | conflict or NEEDS_FACT; no approval silently carried forward |
| classification of a non-existent subject | rejected |
| unverified expense with a classification | excluded from deductible totals, counted unresolved |
| locked workspace | every write path raises `WorkspaceLockedError` |
| unclassified transaction in scope | `unresolved_count` reflects it |
| corrected document | the persisted run becomes `STALE` in the same transaction |
| ledger deletion | run, item and packet all stale, together |
| superseded classification | absent from totals and current allocation arithmetic; retained as history |
| identical split replay / changed split | one complete active revision; history never counted twice |
| two sessions reviewing one subject | one current head; loser returns an identical retry or a conflict |
| write failure after supersession/invalidation | prior decisions, rows and readiness flags all restored |
| missing investment basis | unknown; proceeds never substituted for basis |
| property/subject from another workspace | refused, including allocation parts |
| rejected/superseded history | excluded from pending completeness counts |
| new scoped transaction/property/evidence | scope/readiness and old results updated or invalidated |
| unconfigured / document-only scope | explicit outcome, never inferred account scope |
| scope changed then reopened | same persisted members/version; old runs stale with original scope retained |
| final packet with incomplete evidence/comparisons | refused; labelled draft remains available |

Acceptance target: every invariant above demonstrated, plus the relevant
migration/backup tests and the full unit gate. Test count itself is not a goal.
No existing test should be weakened or deleted; if one asserts
behaviour this change deliberately alters, it is updated and the reason
recorded in the work log.

## 7. Risks worth naming

1. **API change.** `classify()` loses its `amount_cents` argument and
   `bank_flow_totals` reads configured scope. Existing tests, `split()` and
   `compute_run()` use the current APIs and must be migrated; no tax review UI
   calls them yet, so this is cheap now and expensive after TAX-08.
2. **A "no-op replay" could mask a real change.** Mitigated by comparing the
   full decision tuple, not just the treatment. A reviewer may still consider
   this too permissive.
3. **Completeness reporting will change existing numbers.** Once unclassified
   rows surface as unresolved, some workspaces that looked complete will report
   gaps. That is the point, but it will look like a regression to anyone
   watching totals.
4. **Superseded rows accumulate.** Intentional — the audit trail is the
   product — but a workspace reviewed over many corrections will hold history,
   and no retention policy exists yet.
5. **One invalidation path is a shared dependency.** Everything that changes
   evidence now goes through one function. A caller that forgets it reintroduces
   the original bug class, so the fix should be structural where possible, not
   only conventional.

## 8. Implementation handoff

1. Add migration and service contracts for scope, one current decision head and
   revision/source metadata. Preserve existing rows; ambiguous duplicate or
   incomplete legacy groups become NEEDS_FACT, never silently deleted/approved.
   Existing scopes start UNCONFIGURED. Test upgrade and backup version handling.
2. Implement subject resolution, whole-decision replay/replacement, atomic splits,
   strict eligibility/lock checks and concurrent request protection.
3. Implement scope-driven completeness, typed buckets and current-revision-only
   allocation readers. Missing measures remain explicit unresolved evidence.
4. Centralize transactional invalidation across evidence, decision, scope and
   ledger changes; strengthen final packet gating and hashes to use the same
   current revision/scope checks. These are review-snapshot integrity changes,
   not the TAX-08 export UI.
5. Turn the reproductions into regressions, including failure injection and
   separate-session concurrency tests, then run the affected and full gates.

Use one owner for migrations and shared models. UI/PDF work can continue in
parallel in its existing paths. TAX-06 is **implemented, hardening pending**;
this document resolves design choices but does not mark implementation complete.
