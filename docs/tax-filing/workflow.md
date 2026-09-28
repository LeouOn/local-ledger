# Tax filing workspace

Use TaxAct to prepare and submit the return, source documents to establish
reported facts, and Local Ledger to review and reconcile transactions. Local
Ledger budget categories do not establish tax treatment or deductibility.

## Confirm scope before entering figures

- Tax year, federal/state jurisdictions, and filing status.
- TaxAct Online or Desktop and sections already completed.
- Income types, dependents, significant changes from the prior year.
- Extension filed, payments already made, and applicable filing deadlines.

Do not assume that “this year” identifies the tax year.

## Private records

Keep actual forms, returns, completed trackers, and extracted text in a private
folder outside this repository. These templates contain no personal records.
Use separate folders for originals, working papers, TaxAct exports, and filing
confirmations. Preserve originals and distinguish corrected forms from originals.

Local-model processing of raw tax documents is a proposed workflow, not an
existing Local Ledger document-ingestion feature. Its current chat uses budget
aggregates. Do not paste raw tax documents into a cloud evaluation path.

## Work through five stages

1. Inventory documents. Copy document-register.csv into the private folder.
   Compare the prior return with the current year's circumstances to identify
   expected forms. Mark each document missing, received, verified, or entered.
2. Reconcile. Import relevant bank/card CSVs into Local Ledger and resolve
   duplicates and transfers. Use tax forms for form-box amounts; net payroll
   deposits are not W-2 wages. Separate transaction reconciliation from tax
   classification. Record differences and avoid counting the same income twice.
3. Resolve questions. Track uncertainties in questions.csv. For each tax
   treatment decision, record the facts and applicable tax-year IRS/state
   guidance, or the answer from a qualified preparer. A model suggestion is
   not a verified decision. Keep unresolved items out of approved totals.
4. Enter and cross-check. Enter/import documents in TaxAct, verify against
   the originals, and use reconciliation.csv to compare working-paper totals
   with the corresponding TaxAct sections. Mark an item complete only after
   checking the entered amount. Do not assume generic CSVs import into TaxAct;
   supported formats depend on the product and type of information.
5. Review and file. Review income, withholding, estimated/extension payments,
   deductions, credits, and state allocations as applicable. Explain material
   differences from the prior return. Resolve TaxAct review errors. The user
   reviews the final return and explicitly authorizes submission. Save the
   return, payment arrangements, and separate federal/state acceptance notices;
   transmission alone is not acceptance.

## Useful local-model tasks

- Suggest document labels and identify missing checklist items.
- Propose transaction classifications for human review, citing transaction IDs.
- Explain reconciled totals using source references, without inventing figures.
- Turn unresolved discrepancies into focused questions.

Calculate totals deterministically. If document retrieval is added later, every
extracted value should retain its filename, page, and form/box reference, and
require verification against the original. Keep retrieved document text separate
from executable instructions and retain the app's local/cloud boundary.

## Session handoff

At the end of each work session, record: completed sections, missing documents,
unresolved discrepancies, and the next three actions. Store this with the private
records, not in the repository. No automatic filing or financial transactions.

## Sources checked September 14, 2026

- IRS filing dates: https://www.irs.gov/filing/individuals/when-to-file
- IRS extensions: https://www.irs.gov/newsroom/if-you-need-more-time-to-file-request-an-extension

For most calendar-year 2025 federal individual returns, the ordinary filing
deadline was April 15, 2026; a timely extension generally provides until October
15, 2026 to file, not additional time to pay. Confirm individual exceptions and
state deadlines once the filing scope is known.
