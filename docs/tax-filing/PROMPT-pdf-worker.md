You are working on **one narrow, independent fix** in the Local Ledger repo
(`/home/yl/proj/local-ledger`): make PDF text extraction *interruptible* so a
hostile or malformed document cannot block tax document intake indefinitely.

Work as an agent, not a conversational assistant: investigate, implement, test,
report. Do not ask for confirmation on routine decisions — make the obvious call
and record it in your work log.

## Context

The app ingests tax PDFs (1099s, rental statements) and extracts text per page
with pypdf. That works. But the time limits are **observational, not
interruptible**: the current code measures elapsed time *after* `extract_text()`
returns, and truncates output *after* it has already been allocated. A synthetic
100 ms extraction with a 2 ms budget still took 103 ms before returning. Worse,
the initial parse and page-tree traversal happen outside the measured budget
entirely. So a document that hangs or decompresses to something enormous can
block intake — which is an availability bug in a tool whose whole purpose is to
be safe to point at untrusted files.

The fix is a **worker process with enforced wall-time and memory limits**. Note:
switching PDF libraries would not fix this; the problem is that a library call
in-process cannot be interrupted.

## Files you own (create these; do not edit others)

- `src/local_ledger/tax/pdf_worker.py`  — new
- `tests/unit/tax/test_pdf_worker.py`   — new
- `docs/tax-filing/pdf-worker-log.md`   — new work log

## Files you must NOT edit (another agent is actively working in them)

- `src/local_ledger/tax/pdf_text.py`     — **read-only for you**
- `src/local_ledger/tax/intake.py`       — read-only
- `src/local_ledger/tax/classification.py`, `scope.py`, `reconciliation.py`
- `src/local_ledger/tax/service.py`, `models.py`, `backup.py`
- `src/local_ledger/models.py`, `pyproject.toml`, `alembic/**`
- `app_pages/**`, and every other test file

Shared working tree: several agents edit concurrently. **Do not commit**, and do
not reformat or "tidy" anything outside your files. If you believe a file you do
not own has a bug, write it up in your work log instead of changing it.

## The seam you must preserve

`src/local_ledger/tax/intake.py` calls exactly one thing:

```python
from local_ledger.tax.pdf_text import PageStatus, extract_pages
...
result = extract_pages(pdf_bytes)   # -> ExtractionResult
```

`extract_pages` accepts an optional `extractor: PageTextExtractor | None`. Your
job is to provide a worker-backed implementation of that same protocol and make
it the default, **with no change to `intake.py`**. If you find yourself needing
to edit `intake.py` or `pdf_text.py`, your design is wrong — stop and report.

Read `src/local_ledger/tax/pdf_text.py` first. You must preserve:

- `ExtractionResult` / `PageText` / `PageStatus` field names and meanings
- `PageStatus` values: `EXTRACTED`, `NO_TEXT_LAYER`, `TRUNCATED`, `TIMED_OUT`,
  `SKIPPED_AFTER_LIMIT`, `ERROR`
- `is_scanned` true **only** when no page yielded text
- A page with no text must say so **on that page** and must never contain
  another page's text
- `document_note`, `truncated`, `error` semantics
- The guarantee that the **stored original PDF is never modified or discarded**
  by extraction, and that a failure still routes to manual field entry

## Requirements

1. **Real process isolation.** Run parsing and `extract_text` in a worker
   process (`multiprocessing` with `spawn`, or a `subprocess`), so a hang is
   *killed*, not observed. Enforce wall-clock by terminating the worker.
2. **Memory limit** via `resource.setrlimit(RLIMIT_AS)` where the platform
   supports it; degrade explicitly and say so on platforms that do not (macOS
   in particular). A decompression bomb must fail, not exhaust the box.
3. **Per-page granularity preserved.** Page N's text is page N's text. Return
   partial results for pages that completed, and mark the rest `TIMED_OUT` or
   `SKIPPED_AFTER_LIMIT` — a timeout must not discard pages that succeeded.
4. **Never hang the caller.** A wall-clock budget that is genuinely enforced, so
   a caller passing a 2-second budget is back in ~2 seconds, not whenever the
   PDF finishes. Measure this in a test.
5. **Bounded pickling.** Send only the bytes in, get a small structured result
   back. Do not pickle a `PdfReader`.
6. **Failures are information, not crashes.** Worker death, OOM, timeout and
   malformed input each produce a result with a reason and a status — never an
   exception escaping to the UI, never a silent empty success.
7. **No new dependencies.** `pypdf` is already installed and declared. Standard
   library only otherwise.

## Tests (this is the acceptance bar, not a nice-to-have)

Synthetic PDFs only, from the existing builder `tests/unit/tax/pdf_fixtures.py`
(read it, don't copy it). You will need to construct extra adversarial inputs —
a page whose content stream is a large compressed blob, and a page designed to
take a long time.

- a worker that never returns is **terminated** within the budget, and the
  caller observes the timeout (assert wall-clock, with tolerance)
- a large-decompression input hits the memory limit and returns a reason
- pages that completed before a timeout are still returned, correctly attributed
- **worker termination**, not just a status flag — assert no orphaned process
  remains
- a normal multi-page PDF round-trips identically to `PypdfPageTextExtractor`
  output, so the swap-in changes no result
- an encrypted / corrupt / empty PDF still yields `error` with a reason
- the existing suite stays green: run
  `.venv/bin/python -m pytest tests/unit -q` and
  `.venv/bin/ruff check src tests`

## Definition of done

1. All new tests pass **and** the full unit suite passes (currently 486 passed,
   10 skipped — that number should only go up).
2. `ruff check src tests` clean.
3. `extract_pages()` returns the worker-backed extractor by default with no
   change to any file you do not own.
4. `docs/tax-filing/pdf-worker-log.md` records: what you measured first, the
   design, the platform caveats (especially memory limits on macOS), the test
   evidence with actual numbers, and anything you found but deliberately did not
   change.

## Judgement calls to make yourself

Whether the worker is spawned per call or pooled; whether to keep the in-process
extractor as a documented fallback and when to use it; the default budgets. Make
a call, justify it in the log, and move on. Prefer the simpler design unless
there is a concrete reason — the current in-process extractor works correctly
for normal documents and should not become less trustworthy.
