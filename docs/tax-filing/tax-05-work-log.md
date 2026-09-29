# TAX-05 work log — document intake and manual verification UI

Agent B, completed September 29, 2026. Status: **COMPLETE**.

This log records the implementation of document intake, expected vs. missing inventory tracking,
safe local file intake, text/scanned document detection, and the manual field entry & verification UI.
Acceptance criteria are in [tasks.md](tasks.md#tax-05--build-document-intake-and-manual-verification-ui).

## Goals & Outcomes

1. **Document Inventory & Expected/Missing Tracking (`tax/intake.py`):**
   - Implemented `EXPECTED_2025_CATEGORIES` defining required and optional forms for the 2025 review scope:
     - Required: Form 1099 Consolidated, Rental Annual Statement, Form 1098 Mortgage, Property Tax Assessment/Bill.
     - Optional: 2024 Federal 1040, 2024 California 540, Settlement/Closing Statement (HUD-1), Other Supporting Evidence.
   - Implemented `get_inventory_status(session, workspace_id)` returning `InventoryStatus` entries with:
     - `status`: `PRESENT`, `MISSING`, or `SUPERSEDED`.
     - Active document counts and attached document records.
     - Dynamic inclusion of custom-categorized uploaded documents (`Custom: <doc_type>`).

2. **Safe Local File Intake & Validation:**
   - Strict size limit: enforces `MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024` (25MB limit); raises `FileOversizedError`.
   - Empty file rejection: 0-byte uploads raise `EmptyFileError`.
   - Format validation: restricts uploads to `.pdf`, `.json`, `.csv`, `.txt`, `.png`, `.jpg`, `.jpeg`; raises `UnsupportedFileTypeError`.
   - Atomic failure: parsing or validation errors leave zero half-registered records or documents in the database.
   - Content-addressable SHA-256 deduplication: re-uploading identical file bytes returns existing document record with `is_duplicate=True` without creating duplicate records or duplicating approved amounts.
   - Supersession support: replacement uploads with `is_corrected=True` and `supersedes_doc_id` atomically mark previous document fields as `SUPERSEDED` and mark downstream items stale.

3. **Text PDF Inspection & Scanned Document Detection:**
   - Implemented hermetic, dependency-free PDF stream parser in `extract_pdf_page_texts(pdf_bytes)`:
     - Parses PDF catalog page structure and decompresses `FlateDecode` streams with `zlib`.
     - Extracts text tokens from PDF operators `(text) Tj` and `[(text)] TJ`.
     - Distributes text streams into 1-indexed page dictionary.
   - Scanned PDF detection: if a PDF has no extractable text layer or is an image (`.png`, `.jpg`), assigns `ScanState.SCANNED_NO_TEXT` with page warnings guiding the user to manual entry.
   - Never produces silent empty success.

4. **Manual Field Entry & Verification Workflow:**
   - Implemented `add_manual_field`: records proposed manual field values with verbatim `raw_text`, exact integer `amount_cents` (zero floats), page number, box/line, snippet, and optional notes.
   - Implemented `review_field`: applies reviewer action (`VERIFY` or `REJECT`) with timestamp and reviewer attribution.
   - Strict adherence to TAX-04 approval invariant:
     - Only `VERIFIED` fields enter approved calculations.
     - `PROPOSED`, `REJECTED`, and `SUPERSEDED` fields remain strictly excluded from approved totals.
   - Implemented `get_workspace_fields_summary`: tallies approved, proposed, rejected, and superseded cents and counts.

5. **Streamlit Review Page & Navigation:**
   - Implemented `app_pages/tax_intake.py` with:
     - Year selector (default 2025) and jurisdiction selector (`US-FED` vs `US-CA`).
     - Workspace lock state warning.
     - Top KPI metrics: Required Docs checklist progress, Verified Total ($ cents), Proposed Fields count, Total Evidence count.
     - Tab 1: **Document Checklist**: Expected 2025 document inventory table with status badges (`PRESENT`, `MISSING`, `SUPERSEDED`).
     - Tab 2: **Upload Evidence**: Local file uploader, category selector, replacement/correction checkbox, and ingestion feedback.
     - Tab 3: **Document Inspector**: Document selector, metadata summary, SHA-256 hash display, and page-by-page extracted text viewer.
     - Tab 4: **Verification Queue**: Filterable evidence review queue with inline "Verify" and "Reject" buttons for `PROPOSED` fields, enforcing approval invariant.
     - Tab 5: **Manual Entry**: Form to propose fields from scanned or physical source evidence with preset categories, box/line, page #, verbatim text, and exact dollars/cents conversion.
   - Wired navigation in `app.py`:
     - Added `st.Page("app_pages/tax_intake.py", title="Tax Intake", icon=":material/inventory:")`.

6. **Hermetic Test Suite:**
   - `tests/unit/tax/test_tax_intake.py`: 8 comprehensive tests covering inventory tracking, custom documents, file validation (empty, oversized, unsupported), atomic rejection, SHA-256 deduplication, text vs scanned PDF inspection, manual field review lifecycle, and document supersession.
   - `tests/unit/test_ui_smoke.py`: Added `test_tax_intake_page_renders(apptest)` verifying hermetic child process page rendering without errors.

## Acceptance Criteria Verification

- [x] **Synthetic text PDF and scanned PDF journeys retain originals:** Stored immutably with SHA-256 in private storage outside git repo.
- [x] **Duplicate upload does not create duplicate approved amounts:** Content-addressable hash deduplication returns existing document without duplicating records or approved totals.
- [x] **Manual fields can be verified against a visible source:** Text stream preview displays verbatim source text per page; manual entry records verbatim text, page #, and box/line.
- [x] **Missing documents and unsupported types remain visible:** Expected document inventory clearly flags missing required forms; unsupported formats or scanned documents show explicit warnings.
- [x] **Parsing failures leave no half-registered approved records:** Ingestion validates before committing; validation errors rollback and return clean error messages.
- [x] **Zero cloud egress:** All document handling and text extraction execute locally.
- [x] **Strict money invariants:** All monetary values stored and computed strictly as integer cents.
