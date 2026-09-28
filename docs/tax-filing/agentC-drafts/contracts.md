> **DRAFT - NOT THE ACCEPTED CONTRACT. DO NOT BUILD ON THIS.**
>
> This is a second, independent contract draft written by a different agent on
> September 28, 2026 after agent B had already completed TAX-03. It
> **overwrote** agent B's `docs/tax-filing/contracts.md`, which was untracked
> and is therefore unrecoverable. It was moved here so it cannot be mistaken
> for the real contract, and so the canonical path is free for agent B to
> restore.
>
> It is **materially incompatible** with the tax schema agent B is already
> building in `src/local_ledger/tax/models.py` (table names, ID prefixes,
> jurisdiction shape, and several amount fields that default to `0` where this
> draft requires an explicit unknown). Treat this file as design input and
> review material only. See [../COLLISION-2026-09-28.md](../COLLISION-2026-09-28.md).

# Tax workspace contract — 2025 federal/California review (agent C draft)

Created September 28, 2026 by agent C for **TAX-03**. This is the single
contract the tax features build against. It defines scope, invariants, field
shapes, identifiers, state transitions and the service boundaries.

**Authority:** this document is a *software* contract. It contains no tax
treatment rules and no tax advice. Where a decision depends on tax law, the
contract names the decision and requires an **authority record** (see
[§17](#17-decisions-that-require-applicable-year-authority)); the law itself
comes from the applicable-year source, never from model memory or from a
developer's recollection.

**How to use it:** TAX-04 implements the DTOs and tables in
[§14](#14-dto-reference) and [§16](#16-table-inventory-tax-04s-schema) verbatim.
TAX-05 through TAX-15 consume the service boundaries in
[§15](#15-service-boundaries). If an implementation must deviate from a rule
here, the deviation is recorded in this file in the same commit - not in a
code comment. Later agents need no competing migration or competing DTO.

---

## 1. Confirmed scope

Confirmed with the user on September 28, 2026. This is a review and comparison
workspace for figures the user enters into TaxAct. It does not compute a
return and it does not file one.

| Item | Confirmed value |
| --- | --- |
| Tax year | 2025 |
| Jurisdictions | Federal and California |
| Product under review | **TaxAct Online** |
| Residency (2025) | **Full-year California resident** |
| Income areas | Investment income and rental income |
| Rental properties | All located in California; **entity or complex ownership** (LLC/partnership/trust/joint - see [§3](#3-open-scope-questions)) |
| Investment instruments | 1099-INT, 1099-DIV, 1099-B, stock/fund sales needing basis and holding period, and **digital assets (Form 1099-DA)** |
| K-1s (partnership/S-corp) | **Out of scope** - see the conflict in [§3](#3-open-scope-questions) |
| Prior-year (2024) documents | **Not available.** Every prior-year carryover and prior depreciation is therefore `UNKNOWN`, never zero |
| Filing status | Not confirmed - not required by the foundational APIs |
| Preparer | User, assisted by this app; TaxAct computes and files the return |

Priorities for document intake, in order: consolidated brokerage 1099s
(INT/DIV/B, then 1099-DA), rental statements and expense detail, then
depreciation/carryover schedules when they exist.

## 2. Explicit exclusions

Out of scope for this milestone. Each is a hard exclusion, not a "later":

| Excluded | Re-open when |
| --- | --- |
| Tax computation and form filling | Never in this milestone; TaxAct computes the return |
| Electronic filing | Never in this milestone |
| K-1 / partnership / S-corp income | The user confirms entity-held rental income arrives as a K-1 ([OPEN-1](#3-open-scope-questions)) |
| Real property sales and 1099-S | The user confirms a 2025 disposition |
| Excise, AMT, phase-outs, credits beyond those the user asks about | The user asks |
| State jurisdictions other than California | The user adds a property in another state |
| Any cloud transmission of tax data | Never - see [§18](#18-privacy-boundary) |

## 3. Open questions

Explicit unknowns. A feature that depends on one of these must show it as
unresolved rather than proceed on a guess.

| ID | Question | Impact if unanswered | Blocking |
| --- | --- | --- | --- |
| OPEN-1 | **Entity-owned rentals may deliver income as a K-1, and K-1 is out of scope.** The user confirmed complex/entity ownership but did not select K-1s as an investment type. Does 2025 rental income reach the user as K-1 income from an entity, or as direct Schedule E income? | Determines whether TAX-14 reconciles entity-level documents or personal ones. Guessing produces a working paper for the wrong return path | Blocks TAX-14 scope finalisation; not the foundational APIs |
| OPEN-2 | Which entity/entities, and is the property held in the entity, by an individual, or both? | Ownership and use allocations cannot be built | Blocks TAX-14 |
| OPEN-3 | No 2024 documents. Are 2024 federal/CA returns, depreciation schedules and carryover statements obtainable (from a preparer, or from TaxAct's own prior-year data)? | All carryover and prior-depreciation fields stay `UNKNOWN`; the packet must show them as open questions | Blocks any carryover-dependent verified total |
| OPEN-4 | Are there wash sales, straddling or short sales, or options activity? | Holding-period and adjustment questions cannot be classified | Affects TAX-13 field coverage |
| OPEN-5 | Digital assets: which wallets/exchanges/custodians, and were any transfers, staking, lending or income events involved? | The expected 1099-DA set is unknown; digital-asset disposal rules are authority-gated | Affects TAX-13 field coverage |
| OPEN-6 | Filing status and number of dependents/credits | Only affects packet presentation, not the data model | Non-blocking |

**OPEN-3 is the one that shapes data most:** with no prior-year documents, a
carryover that is absent from the workspace is *unknown*, and the packet must
say so rather than treating a missing carryover as zero.

## 4. Invariants

These are the rules every feature must hold. Each is testable; the citation is
the test that should exist when the feature lands.

| ID | Invariant |
| --- | --- |
| INV-1 | **Money is integer cents.** No float, ever, in storage, transport or arithmetic. A source decimal is converted once, exactly, and the original text is kept beside it |
| INV-2 | **Unknown is not zero.** A missing value is `NULL` plus an `unknown_reason`. Missing cost basis, missing prior depreciation and unknown residency are `UNKNOWN` - never `0`, never "assumed recoverable" |
| INV-3 | **Confidence is not verification.** A model proposal, a rule hit or a confidence score never puts a value into an approved total. Only a verified value does |
| INV-4 | **Every amount has a source.** Each amount links to at least one `SourceLocator` (document version + page/box/field) or is explicitly `MANUAL_ENTRY` with a named reviewer |
| INV-5 | **Year isolation.** Every tax row carries `tax_year`, and every read filters on the selected workspace's year. A 2024 value can never appear in a 2025 total, not even through a join |
| INV-6 | **Federal and California are separate records.** A federal value is never copied to a California value. Differences are the point |
| INV-7 | **Budget category is not tax treatment.** A ledger category name is never a treatment decision. Treatment lives in `TaxClassification` with a recorded fact and authority (see [§13](#13-tax-category-is-not-a-tax-treatment)) |
| INV-8 | **Arithmetic is deterministic.** Totals, deltas and comparisons are computed in code. No model participates in arithmetic, ever |
| INV-9 | **Originals are immutable.** A stored original is never edited. A correction is a new version that supersedes the old one |
| INV-10 | **Supersession invalidates downstream.** When evidence changes, any approved total, snapshot or packet computed from the old version is marked `STALE`, not silently reused |
| INV-11 | **A skipped case is visible.** Absent, unreadable, unsupported or unverified input appears as an explicit item. "No data" is never rendered as an empty success |
| INV-12 | **Raw tax data never enters a cloud payload** ([§18](#18-privacy-boundary)) |
| INV-13 | **Extraction proposes; humans verify.** A local model may propose a value with a source locator. It may not verify, approve, or choose a treatment |
| INV-14 | **Scope errors stay visible.** A form, box or screen outside the supported scope is an explicit unsupported item, never a guess and never a silent omission |

## 5. Money and conversion

- Storage: `*_cents INTEGER`, signed, matching the ledger's cents convention
  (income positive, expense negative) for transaction-linked amounts. Document
  totals are stored with the sign as printed on the form; a sign convention per
  field kind is declared in the field registry, not inferred.
- Conversion from a printed amount is done by an exact decimal-to-cents routine
  with a *strict* parser (the existing `ingest/amounts.py` rules apply: no
  binary-float intermediates, no silent rounding, ambiguous formats rejected).
- The printed text is always retained (`raw_text`) beside the converted value.
  Conversion is a pure function of the text, so a conversion can be re-verified
  and re-run when the parser is improved.
- A value that cannot be converted exactly is `UNKNOWN` with a reason, not
  `0` and not a rounded guess.
- Whole-dollar presentation (some form lines) is a display concern. Storage and
  comparison stay in cents.
- Percentages, quantities and share counts are not money: `ratio_bp` (basis
  points, integer) and `quantity_milli` (thousandths, integer) are used where a
  decimal would otherwise appear, so no float enters the schema.

## 6. Field types

A small closed set. Every extracted or entered value has exactly one type.

| Type | Storage | Rule |
| --- | --- | --- |
| `money` | `*_cents INTEGER` | Signed; converted per [§5](#5-money-and-conversion) |
| `date` | `TEXT` `YYYY-MM-DD` | ISO; a form's own date format is kept in `raw_text` |
| `tax_year` | `INTEGER` | The workspace's year |
| `ratio` | `*_bp INTEGER` | Basis points; never a float |
| `quantity` | `*_milli INTEGER` | Thousandths; never a float |
| `text` | `TEXT` | Bounded length, never used for identity |
| `identifier` | `TEXT` | Stable, opaque, prefixed ([§7](#7-identifiers)); never reused |
| `enum` | `TEXT` | Value of a registered enumeration; an unregistered value is an explicit `OTHER` + `raw_text` |
| `flag` | `INTEGER` 0/1 | Only for facts the source states; absence is `NULL`, not `0` |
| `unknown` | `NULL` + `unknown_reason TEXT` | The first-class representation of "not known" |

An `enum` value must be registered in the field registry before it can be
stored. Registration is a code change with a version bump ([§13](#13-export-and-registry-versioning)),
which is what stops a model from inventing a treatment code.

## 7. Identifiers

Opaque, stable, never reused, never renumbered. Format `<prefix>_<token>` where
the token is a stable slug for fixtures and a random id in production.

| Prefix | Entity | Notes |
| --- | --- | --- |
| `ws_` | TaxWorkspace | One per (tax_year, jurisdiction set, preparer mode) |
| `doc_` | TaxDocument | A logical document (e.g. one brokerage 1099-B package) |
| `docv_` | TaxDocumentVersion | An immutable stored file: hash, path, supersedes |
| `loc_` | SourceLocator | page/box/field/row/column within one document version |
| `fld_` | FieldValue | One value for one field of one document version |
| `prop_` | SourceProposal | A model-proposed value, pre-verification |
| `ver_` | VerificationEvent | One human decision, append-only |
| `txn_` | Ledger transaction reference | Points at the existing `transaction.id`; never a copy |
| `prop_`/`asset_` | Rental property, investment asset | `prop_` for real property, `asset_` for a security lot |
| `sale_` | InvestmentSaleRecord | proceeds + basis + dates + classification, kept separate from cash flows |
| `carry_` | CarryoverRecord | always `jurisdiction`-scoped (federal or CA), never shared |
| `cls_` | TaxClassification | treatment decision + facts + authority |
| `q_` | TaxQuestion | open/unresolved item |
| `run_` | ReconciliationRun | a computed, hash-identified snapshot |
| `auth_` | AuthorityRecord | applicable-year source for a treatment decision |
| `pkt_` | PacketManifest | a versioned export |
| `ct_` | TaxActEntryComparison | one line of a review packet |

An identifier is never a display label, never a row number, and never derived
from content that can change (a filename, a payee name, a document title).

## 8. Year and jurisdiction isolation

- `TaxWorkspace.tax_year` is fixed at creation. A workspace is for one year and
  one set of jurisdictions.
- Every tax table carries `workspace_id` and `tax_year`; every query filters on
  the workspace. Cross-year reads are a bug, not a feature (INV-5).
- `Jurisdiction` is a registered enumeration (`FEDERAL`, `CALIFORNIA`) so a
  second state is a registry change, not a schema change.
- Federal and California records for the same fact are separate rows keyed by
  `(workspace_id, field_key, jurisdiction)` (INV-6).
- A packet export declares the year and jurisdiction set it covers and refuses
  to mix them.

## 9. State transitions

### Document version

```
registered ──supersede──> superseded (terminal, kept)
    │
    └──never edited (INV-9)
```

`registered` rows also carry a *scan state*: `text_extractable`,
`scanned_no_text`, `unsupported_type`, `too_large`, `hash_duplicate`,
`workspace_mismatch`. Every non-`text_extractable` state is visible in the UI
and blocks the field table for that document (INV-11). OCR is an explicitly
scoped extension: a `scanned_no_text` document offers manual entry; it never
reports an empty success.

### Field value

```
proposed ──verify──> verified ──┐
   │                            │
   ├──reject──> rejected        ├──supersede──> superseded (terminal)
   │                            │
   └──abandon─> abandoned       └── (source changed) ──> stale
```

- Only `verified` contributes to an approved total (INV-3).
- `rejected` records who rejected it and why; it is never silently deleted.
- `superseded` and `stale` rows are retained for the audit trail and excluded
  from totals.
- A value can be `verified` with an explicit `MANUAL_ENTRY` source (INV-4) when
  the document is unreadable, provided a reviewer is named.

### Classification

```
unclassified ──classify──> classified ──> needs_fact ──> classified
                     └───> unresolved (blocking a "fully checked" state)
```

A classification carries the fact that supports it and, where treatment depends
on law, an `authority_id`. A classification with no fact is `unresolved`, not a
default.

### Question

```
open ──answer──> answered ──> closed
  └───> wont_fix (recorded, still visible)
```

An open question blocks "fully checked" for anything it touches.

### Packet

```
draft ──(reviewed snapshot present)──> approved
   └──(snapshot stale)──> stale
```

A draft can be exported at any time and is labelled `DRAFT`. `approved` requires
a `run_id` whose content hash still matches the workspace state; otherwise the
packet is `stale` and cannot be presented as approved (INV-10).

## 10. Corrections, supersession and staleness

1. A corrected document is a **new version** with `supersedes_id` set and a new
   `sha256`. The original stays on disk and stays queryable.
2. Field values, classifications, reconciliation runs and packets that depended
   on the superseded version are marked `stale` in the same transaction that
   records the supersession (INV-10).
3. A ledger edit invalidates the affected approvals: a change to a transaction
   that a reconciliation run consumed marks that run stale. The link is
   explicit - reconciliation totals carry the `txn_` ids they consumed
   ([§14](#14-dto-reference)), so invalidation is computed, not guessed.
4. Re-running reconciliation produces a new `run_` with a new content hash.
   Older runs are retained, never overwritten.

## 11. Source integrity

- `TaxDocumentVersion.sha256` is computed over the stored bytes at registration
  and re-verified on read. A mismatch is a hard error, never a warning.
- Duplicate detection is by hash: re-uploading identical bytes does not create
  a second document and therefore cannot double an approved amount.
- The hash is the identity for "the same file", not the filename, and not the
  document title.
- Private storage lives outside the tracked source tree; the absolute path is
  stored, the raw values are not, and neither appears in ordinary error logs.

## 12. Model provenance and trust

- A proposal records `model_id`, `prompt_version`, `endpoint`, `created_at`,
  `latency_ms` and the source locator it claimed.
- Provenance is descriptive, not authoritative: it records *who proposed* and
  *from what*. A model id never appears in a tax determination, and a proposal
  is never auto-verified (INV-13).
- Hallucinated or unresolvable source locators are rejected at the boundary, or
  recorded as `unresolved` - never silently accepted.
- Document text is untrusted data: it cannot change the workflow, approve a
  value, or cause an external call.
- Extraction quality is qualified separately (TAX-10) on synthetic fixtures.
  An unqualified model stays experimental and manual preparation still works.
- A prompt or schema version bump is recorded with the proposals it produced, so
  a historical proposal remains explainable.

## 13. Tax category is not a tax treatment

The app's `Category` is a *budgeting* dimension. Tax treatment is a separate
axis with a different vocabulary and a different lifecycle:

| | Budget category | Tax treatment |
| --- | --- | --- |
| Vocabulary | User's own ("Dining", "Fun") | Registered code (`RENTAL_REPAIR`, `TRANSFER`, `INCLUDIBLE_OTHER`) |
| Who assigns it | Rules, LLM suggestion, user | Requires a recorded fact, plus authority where law decides |
| Meaning | Where money went in a budget | How the amount is treated for a return |
| Failure mode | A wrong guess is cosmetic | A wrong guess changes a filed return |

So: no mapping from category to treatment is derived implicitly. If a rule or a
model suggests a treatment, it produces an `unclassified`/`unresolved`
classification with the category recorded as *supporting context only*, and it
never becomes a verified treatment without a fact and, where required, an
authority record. Transfers and refunds are excluded from income totals on
their own explicit classification, never because of a category name.

## 14. DTO reference

Illustrative Python. These are the shapes TAX-04 implements; a deviation must be
recorded in this file.

```python
Jurisdiction = Literal["FEDERAL", "CALIFORNIA"]
FieldKind = Literal["money", "date", "tax_year", "ratio", "quantity",
                    "text", "identifier", "enum", "flag", "unknown"]


@dataclass(frozen=True)
class TaxWorkspace:
    id: str                      # ws_...
    tax_year: int                # 2025
    jurisdictions: tuple[Jurisdiction, ...]
    created_at: datetime
    schema_version: str          # registry version, see §14.4


@dataclass(frozen=True)
class TaxDocumentVersion:
    id: str                      # docv_...
    document_id: str             # doc_...
    workspace_id: str
    tax_year: int
    kind: str                    # registered DocumentKind, e.g. BROKERAGE_1099_B
    sha256: str
    stored_path: str             # private, outside the tracked tree
    original_filename: str
    page_count: int | None
    scan_state: str              # text_extractable | scanned_no_text | ...
    supersedes_id: str | None
    is_original: bool
    received_at: datetime


@dataclass(frozen=True)
class SourceLocator:
    id: str                      # loc_...
    document_version_id: str     # docv_...
    page: int | None             # 1-based
    form_label: str | None       # e.g. "1099-B box 1a"
    field_label: str | None      # e.g. "Proceeds"
    table_row: int | None
    table_column: str | None
    raw_text: str                # exact text, retained (INV-1)
    char_start: int | None
    char_end: int | None


@dataclass(frozen=True)
class FieldValue:
    id: str                      # fld_...
    workspace_id: str
    tax_year: int
    jurisdiction: Jurisdiction
    field_key: str               # registered in the field registry
    kind: FieldKind
    value_text: str | None       # exact source text
    value_cents: int | None      # money only
    raw_text: str | None
    source_locator_id: str | None      # None only for MANUAL_ENTRY (INV-4)
    state: str                   # proposed|verified|rejected|superseded|stale
    unknown_reason: str | None   # required when state is unknown (INV-2)
    supersedes_id: str | None
    recorded_at: datetime


@dataclass(frozen=True)
class SourceProposal:
    id: str                      # prop_...
    field_value_id: str
    model_id: str
    prompt_version: str
    endpoint: str
    created_at: datetime
    latency_ms: int | None
    source_locator_id: str | None


@dataclass(frozen=True)
class VerificationEvent:
    id: str                      # ver_...
    field_value_id: str
    reviewer: str
    decided_at: datetime
    decision: str                # verify | reject
    method: str                  # manual_entry | compare_to_source
    note: str | None


@dataclass(frozen=True)
class TaxClassification:
    id: str                      # cls_...
    scope: str                   # "txn:<id>" | "field:<id>" | "sale:<id>"
    treatment: str | None        # registered code, or None when unresolved
    state: str                   # unclassified|classified|unresolved|needs_fact
    fact_refs: tuple[str, ...]   # field_value ids that support it
    authority_id: str | None     # auth_..., required where law decides
    reviewed_by: str | None
    reviewed_at: datetime | None


@dataclass(frozen=True)
class InvestmentSaleRecord:
    id: str                      # sale_...
    asset_id: str                # asset_... (a lot/security), not an account
    tax_year: int
    proceeds_cents: int
    basis_cents: int | None      # None = unknown, never 0 (INV-2)
    date_acquired: str | None
    date_sold: str
    term_class: str | None       # registered code, None when unknown
    adjustments_cents: int | None
    adjustment_labels: tuple[str, ...]
    statement_version_id: str    # docv_... lineage
    source_field_ids: tuple[str, ...]


@dataclass(frozen=True)
class CarryoverRecord:
    id: str                      # carry_...
    jurisdiction: Jurisdiction   # federal and CA are separate rows (INV-6)
    carryover_kind: str          # registered, e.g. capital_loss_short
    amount_cents: int | None     # None = unknown when no prior return exists
    unknown_reason: str | None
    prior_return_ref: str | None # docv_... of the prior return, when available
    source_field_ids: tuple[str, ...]


@dataclass(frozen=True)
class RentalProperty:
    id: str                      # prop_...
    workspace_id: str
    tax_year: int
    nickname: str                # user-facing, never an identifier
    state: str
    ownership_kind: str          # e.g. individual | joint | llc | partnership | trust
    ownership_share_bp: int | None
    placed_in_service: str | None
    basis_cents: int | None
    use_allocation_bp: int | None


@dataclass(frozen=True)
class ReconciliationTotal:
    field_key: str
    jurisdiction: Jurisdiction
    amount_cents: int
    member_ids: tuple[str, ...]  # every contributing fld_/txn_/sale_ id (INV-8)
    excluded_ids: tuple[str, ...]  # excluded with a recorded reason


@dataclass(frozen=True)
class ReconciliationRun:
    id: str                      # run_...
    workspace_id: str
    tax_year: int
    as_of: datetime
    content_hash: str
    state: str                   # current | stale
    totals: tuple[ReconciliationTotal, ...]
    unresolved_question_ids: tuple[str, ...]


@dataclass(frozen=True)
class TaxQuestion:
    id: str                      # q_...
    workspace_id: str
    tax_year: int
    text: str
    why_it_matters: str
    resolves_by: str             # what evidence would settle it
    blocking: bool
    status: str                  # open|answered|wont_fix
    related_ids: tuple[str, ...]


@dataclass(frozen=True)
class TaxActEntryComparison:
    id: str                      # ct_...
    field_key: str
    jurisdiction: Jurisdiction
    source_field_ids: tuple[str, ...]
    working_paper_cents: int
    taxact_field_label: str      # only if verified for this edition (INV-14)
    user_entered_cents: int | None
    delta_cents: int | None
    status: str                  # match | mismatch | unentered | unverifiable
    explanation: str
    authority_id: str | None


@dataclass(frozen=True)
class PacketManifest:
    id: str                      # pkt_...
    manifest_version: str
    workspace_id: str
    tax_year: int
    generated_at: datetime
    state: str                   # draft | approved | stale
    reconciliation_run_id: str | None
    snapshot_content_hash: str | None
    comparisons: tuple[TaxActEntryComparison, ...]
    unresolved: tuple[TaxQuestion, ...]
    document_status: tuple[str, ...]
```

### 14.4 Export and registry versioning

Two independently versioned strings, both recorded on every artefact:

- `manifest_version` - the packet export shape (`tax-packet/1`). Bumped for a
  breaking change to the CSV/JSON columns. A consumer must be able to read an
  older manifest; exports are not rewritten in place.
- `schema_version` - the field/treatment/authority registry
  (`tax-registry/1`). Bumped when a `field_key`, `enum` or `DocumentKind` is
  added or changed. A value is stored with the registry version that produced
  it, so a historical value is always interpretable.

An export is reproducible: the same workspace state and the same registry
version produce byte-identical output, except for the timestamps it declares.

## 15. Service boundaries

The seams between agents. Later features call these, not each other's tables.

```python
class DocumentIntakeService:            # TAX-05
    def register(self, *, workspace_id, kind, path, filename) -> TaxDocumentVersion
    def supersede(self, *, new_version_id) -> None
    def find_by_hash(self, *, workspace_id, sha256) -> TaxDocumentVersion | None
    def extract_text(self, *, document_version_id) -> str          # preserves pages
    def scan_state(self, *, document_version_id) -> str

class FieldReviewService:               # TAX-05
    def enter(self, *, field_key, kind, value, source_locator_id | None) -> FieldValue
    def verify(self, *, field_value_id, reviewer, method) -> VerificationEvent
    def reject(self, *, field_value_id, reviewer, note) -> VerificationEvent
    def supersede_fields_of(self, *, document_version_id) -> int      # invalidate

class ExtractionProposalService:        # TAX-07  (local model only)
    def propose(self, *, document_version_id, field_keys) -> list[SourceProposal]
    # never verifies, never selects a treatment, never leaves the machine

class ClassificationService:            # TAX-06
    def classify_transaction(self, *, transaction_id, ...) -> TaxClassification
    def classify_field(self, *, field_value_id, ...) -> TaxClassification
    def unresolved(self, *, workspace_id, tax_year) -> list[TaxClassification]

class ReconciliationService:            # TAX-06
    def compute(self, *, workspace_id, tax_year) -> ReconciliationRun     # deterministic
    def invalidate_for_transactions(self, *, transaction_ids) -> int

class InvestmentAuditService:           # TAX-13
    def reconcile_1099(self, *, document_version_id) -> list[InvestmentSaleRecord]
    def questions(self, *, workspace_id, tax_year) -> list[TaxQuestion]

class RentalAuditService:               # TAX-14
    def reconcile_property(self, *, property_id) -> ReconciliationTotal
    def questions(self, *, property_id) -> list[TaxQuestion]

class JurisdictionService:              # TAX-15
    def carryover(self, *, workspace_id, jurisdiction, kind) -> CarryoverRecord
    def difference(self, *, field_key) -> int | None                 # federal - CA

class PacketExportService:              # TAX-08
    def manifest(self, *, workspace_id, tax_year, state) -> PacketManifest
    def write(self, *, manifest, destination) -> list[Path]           # CSV + JSON + readable

class QuestionService:                  # shared
    def open(self, *, text, why_it_matters, resolves_by, related_ids) -> TaxQuestion
    def answer(self, *, question_id, answer, evidence_ids) -> TaxQuestion
```

`ReconciliationService.compute` and everything under it is pure, deterministic
code. No service above takes an `Actor`-less write: every mutation records a
reviewer or actor, matching the ledger's audit convention.

## 16. Table inventory (TAX-04's schema)

TAX-04 owns these. TAX-03 adds no migration. The inventory is here so later
agents need no competing migration and no second opinion on keys.

| Table | Purpose | Key/uniqueness |
| --- | --- | --- |
| `taxworkspace` | one year/jurisdiction review | unique `(tax_year, jurisdictions_key)` |
| `taxdocument` | logical document | unique `(workspace_id, kind, doc_key)` |
| `taxdocumentversion` | immutable stored file + hash | unique `sha256` per workspace; FK `supersedes_id` |
| `sourcelocator` | page/box/field reference | FK to one document version |
| `fieldvalue` | one value for one field | unique `(workspace_id, jurisdiction, field_key, document_version_id)` per live row |
| `sourceproposal` | model proposal + provenance | FK to `fieldvalue` |
| `verificationevent` | append-only decision | FK to `fieldvalue` |
| `taxclassification` | treatment decision | unique `(scope, state=live)` |
| `authorityrecord` | applicable-year source | unique `(registry_version, citation_key)` |
| `rentalproperty` | property with ownership/use | FK workspace |
| `investmentsale` | proceeds/basis/term per lot | FK asset |
| `carryoverrecord` | federal/CA carryovers | unique `(workspace_id, jurisdiction, carryover_kind)` |
| `taxquestion` | open/unresolved item | FK workspace |
| `reconciliationrun` | computed snapshot + content hash | FK workspace, unique `(workspace_id, tax_year, content_hash)` |
| `reconciliationmember` | total ↔ member id trace | FK run |
| `taxpacket` | export manifest | FK run |
| `taxactentrycomparison` | one packet line | FK packet |

Constraints that are part of the contract, not implementation detail:

- `amount_cents` columns are `INTEGER NOT NULL`; a missing amount is `NULL` plus
  `unknown_reason`, never `0`.
- `tax_year` is `NOT NULL` on every table above.
- `jurisdiction` is `NOT NULL` on every amount-bearing table, and a federal row
  and a California row for the same `field_key` cannot collide (INV-6).
- A `CHECK` that `value_cents` and `value_text` are not both set for the same
  row.
- The ledger's `transaction` rows are referenced, never copied. TAX-01's
  `transaction(account_id, imported_id)` unique index and staged-row provenance
  (migration `e5f7a1c9d204`) remain the entry contract for imported data.

## 17. Decisions that require applicable-year authority

A treatment decision may only be marked `classified` when the decision type
appears here and an `AuthorityRecord` exists for the applicable year and
jurisdiction. Everything else stays `unresolved` with a question. The list is
versioned with `tax-registry/1`; adding an entry is a registry change.

| Decision | Why it needs authority | Typical authority source |
| --- | --- | --- |
| Holding period / short vs long term | Statute and holding-period rules change | applicable-year Form 8949 instructions |
| Wash-sale adjustment treatment | Statutory, with per-broker reporting rules | applicable-year instructions; broker statements |
| Basis method and missing basis | Method and recovery rules are law | applicable-year instructions |
| California basis adjustment | California adjustment applies separately to federal figures | applicable-year CA Schedule D instructions |
| Depreciation method, life, and placed-in-service convention | Method, recovery period and mid-month convention | applicable-year Publication 527 / CA guidance |
| Suspended loss and passive activity limits | Ordering and carryforward rules are law | applicable-year instructions |
| Rental expense deductibility, repairs vs improvement | Distinction is a legal test, not a heuristic | applicable-year Schedule E instructions / Publication 527 |
| Entity vs individual reporting path | Depends on the holder and the entity's tax classification | applicable-year instructions + entity documents (see OPEN-1/OPEN-2) |
| Residency and source allocation | Determines which figures reach the CA return | applicable-year CA instructions |
| Digital-asset disposal treatment and cost-basis recovery | The reporting rules for 2025 digital assets are specific and were phased in | applicable-year IRS digital-asset guidance + 1099-DA instructions |
| Carried-over amounts without a prior return | Cannot be derived from current-year data | prior return or preparer confirmation (OPEN-3) |

An `AuthorityRecord` holds: `citation_key`, jurisdiction, `tax_year`, the
official URL or document identifier, the retrieval date, the specific statement
relied on, and a short note. Model memory is never an authority. A URL that has
not been checked for the applicable year cannot back a `classified` state.

Reference starting points, verified September 28, 2026 for scope only
([readiness assessment](readiness-2026-09-28.md)): Form 8949 instructions,
Schedule E instructions, Publication 527, and California's 2025 Schedule CA and
Schedule D instructions. Exact TaxAct screen guidance is edition-specific and is
out of scope for this contract (INV-14).

## 18. Privacy boundary

- Tax documents, tax values and tax questions are **local-only**. They never
  enter an existing cloud payload, and they are not part of the budget
  aggregate context the chat assistant uses.
- Enforcement is structural, not documentary:
  - tax modules must not import `llm.providers.get_cloud_client`,
    `llm.cloud.send_chat_request`, or `llm.privacy.build_cloud_payload`;
  - a local extraction path uses `llm.providers.assert_local_endpoint` and the
    local client, and fails closed on a non-loopback endpoint;
  - a test asserts the import ban over the whole `tax` package, so the boundary
    cannot be crossed by an innocent import later.
- Private documents are stored outside the tracked source tree, with documented
  access permissions and at-rest assumptions; backups inherit those permissions
  (TAX-09).
- Ordinary error logs carry ids, field keys and reasons - never raw amounts,
  payees, document text or absolute private paths.
- The egress audit log applies to tax paths as it does elsewhere: if a tax
  module ever attempts a cloud call, the attempt is a hard failure, not a
  consent prompt.

## 19. Schema head and ownership

- Current migration head after TAX-01: `e5f7a1c9d204` (staged-row provenance
  columns; partial unique index on `transaction(account_id, imported_id)`).
- **TAX-04 owns `models.py` and the tax schema.** TAX-03 changed no application
  schema, as scoped. TAX-04 rebases onto `e5f7a1c9d204` and adds exactly the
  tables in [§16](#16-table-inventory-tax-04s-schema) - no more.
- No two agents create migrations concurrently. An agent that needs a field or
  table not in [§16](#16-table-inventory-tax-04s-schema) requests it from the
  schema owner rather than adding a competing Alembic head.
