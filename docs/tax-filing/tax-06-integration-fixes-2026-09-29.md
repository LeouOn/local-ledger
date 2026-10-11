# TAX-06 integration fixes — September 29, 2026

The coordinator implemented the integration fixes following the
[PDF/TAX-06 review](integration-review-2026-09-29.md). The subagents' existing
worker, extraction interface, classification history, persisted scope and
migration were preserved. No personal tax records were used and the live
database was not migrated. The working tree remains uncommitted.

## What changed

| Finding | Result |
| --- | --- |
| Cached sessions overwrite a newer decision | Head reads refresh from the database; a conditional UPDATE claims its version before changing parts. Stale requests and competing first-head creation produce review conflicts. Failed reviews roll back parts, head and downstream invalidation without caller cleanup. |
| Rental totals ignore property scope | Workspace reports include selected properties only. Explicit requests for an unselected property are refused. DOCUMENTS_ONLY can select rental properties; UNCONFIGURED cannot claim complete reports. Configured scope also constrains transaction/property classification eligibility. |
| New transactions leave old reports CURRENT | Creation, paired transfers and import commits notify the tax domain within their own transaction. Invalidation finds affected workspaces through account/year scope and source relationships, including formerly empty runs. Deletions preserve old coordinates. Staging, import replay, other accounts and other tax years do not invalidate unrelated runs. |
| Final packets use only the question gate | A reusable deterministic readiness evaluator checks scope and property confirmation, pending evidence, current unresolved bank/rental subjects, unknown investment basis, changed investment decisions, missing replacement evidence, open questions and current comparisons. Drafts remain available with explicit reasons; final designation is refused for gaps. |
| Resolved history is counted as pending | Rejected and superseded fields remain excluded and traceable, without pending counts. Rental history also stays out of unresolved totals. A current corrected document without replacement evidence is a separate readiness gap. |

Two additional cases reproduced during this work: an independently superseded
field still contributed to a cached session's source total, and an independently
changed comparison still passed a cached session's packet gate. Reconciliation
and readiness reads now refresh the relevant rows from the database. Source
fingerprint checks also refresh authoritative source records.

Review mutations use the same head claim and rollback boundary for classification,
splits, marking a subject unresolved and source revalidation. Marking unresolved
creates a new revision and preserves the prior rationale; source revalidation
invalidates downstream results and retains the recorded rationale.

New document/field/expense/sale/property/question writes invalidate the workspace's
derived results. A comparison replacement retains its predecessor as stale
history and stales existing packets. Readiness requires one current, matched,
arithmetically consistent comparison per recorded comparison key; an old stale
comparison can be cleared by recording its new reviewed replacement.

Packets hash their scope, readiness, decision heads/history, property records,
rental expenses, investment sales and comparison history, in addition to their
existing documents, verified fields and questions. Existing immutable snapshots
remain unchanged when current records change.

Scope reads no longer insert or commit an UNCONFIGURED row as a side effect.
An absent scope has an explicit unconfigured default; configuration persists it
in the same transaction as invalidation. Source and investment previews carry
scope metadata and cannot claim completeness without configuration.

## Verification

- The initial regression file reproduced all five findings: **5 failed, 2 passed**.
- Two additional stale-read regressions failed before refreshing the readers.
- Final integration regression file: **22 passed**, with temporary file-backed
  databases and independent sessions. Thread barriers force simultaneous first
  creation and replacement; each race has one winner. Failure injection after
  downstream invalidation restores the prior decision and report/packet flags.
- Tax, importer and ledger checks passed **201 tests** before the final regression
  additions. Existing fixtures were updated to configure genuine review scope
  and comparisons; rejected history expectations were corrected.
- Final complete default suite: **557 passed, 10 skipped, 4 deselected**, 59.73 s.
  Real-service integration cases remain outside this default run.
- Ruff passed for `src`, `tests`, `app.py` and `app_pages`; `git diff --check`
  passed. Alembic still has one head, `e2b47a9c6d10`; no new migration was needed.

## Remaining product work

These fixes establish the service integrity boundary; they do not complete the
TaxAct preparation journey or qualify a local model.

- TAX-05/TAX-06 UI follow-up: configure scope visibly, show the hash-verified
  original page with each proposed field, retain worker status/caveats, replace
  the aggregate Verified Total with meaningful typed summaries.
- TAX-13, TAX-14 and TAX-15 can proceed in parallel: investment checks, rental
  working papers, and federal/California differences and carryovers. Coordinate
  shared model/service edits rather than overlapping file ownership.
- TAX-08 follows those audit tracks: bind every required TaxAct field to its
  current reviewed working-paper snapshot and build comparison/export UI.
  The current service gate checks recorded comparison keys; it does **not** prove
  that every required TaxAct field has a comparison or that an arbitrary caller
  supplied the correct working-paper amount. That stronger coverage/provenance
  check belongs to this field-mapping work.
- TAX-07 and TAX-10 implement and qualify local extraction proposals. Model
  output must remain unverified; arithmetic and readiness remain deterministic.
- History retention and enum lint tooling remain lower priority than the above.
