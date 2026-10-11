# pdf worker work log — interruptible PDF text extraction

Agent E (pdf worker), September 29, 2026. Status: **complete**.

Task: `docs/tax-filing/PROMPT-pdf-worker.md`. Owned files, all new:

- `src/local_ledger/tax/pdf_worker.py`
- `tests/unit/tax/test_pdf_worker.py`
- `docs/tax-filing/pdf-worker-log.md` (this file)

No file owned by another agent was edited. Where I found something in
another owner's file, it is written up below under "Found, deliberately
not changed".

## What I measured first

The defect to fix: `pdf_text.py` measures elapsed time *after*
`extract_text()` returns and the initial parse is outside the budget
entirely, so budgets are observational. Calibration on this machine
before writing any code:

| Measurement | Result |
| --- | --- |
| pypdf `extract_text` on a synthetic N-op content stream | ~36k ops/s: 150k ops → 5.19 s, 200k ops → 7.31 s, in a 56 KB compressed file |
| spawn + `local_ledger.tax` + pypdf import in the child | 0.17–0.23 s per worker |
| child RSS with pypdf imported | ~62 MB (default memory limit therefore 512 MiB: ~450 MiB of real headroom) |
| decompression bomb, 512 MiB expanded / 1.2 MiB compressed, child `RLIMIT_AS` = 64 MiB | `MemoryError` at 0.22 s |
| same bomb, no OS limit | pypdf's **own** cap fires: `LimitReachedError` at 0.29 s |
| pypdf's internal decompression cap | `Configuration.zlib_maximum_output_length` = 75,000,000 bytes (~72 MiB) |
| enforced kill: 2.0 s total budget on the 400k-op slow page | caller back at **2.01 s**, worker exitcode −15 (SIGTERM) |

Two traps found while calibrating, recorded so the next person skips them:

1. **pypdf has its own decompression-bomb guard.** A ~72 MiB output cap
   raises `LimitReachedError` (a catchable per-page error) before an OS
   limit at or above that size can matter. The OS `RLIMIT_AS` is still
   required — it is the only backstop for allocation paths pypdf's cap
   does not cover (object graphs, uncompressed structures, any future
   extractor swap) — but tests that want to prove the OS limit must set it
   *below* pypdf's cap.
2. **Two fixture bugs that impersonate pypdf bugs.** A page dict built as
   two adjacent byte literals at statement level silently parses as an
   assignment plus a no-op expression, producing a page with no
   `/Contents` (extracts empty, instantly). A page without `/Resources`
   also extracts empty almost instantly. Both made my first "slow page"
   and "bomb" look like pypdf was ignoring them. The fixtures in the test
   file build pages as single parenthesized expressions and always attach
   `/Resources`.

Also: `multiprocessing.spawn` re-runs the parent's main module. Under
pytest that is safe (the console script is guarded by
`__name__ == '__main__'`), but `python - <<heredoc` calibration scripts
break spawn outright (`FileNotFoundError: <stdin>`) — calibration must run
from real files.

## Design

`WorkerPageTextExtractor` in `tax/pdf_worker.py` implements the exact
`PageTextExtractor` protocol `intake.py` depends on, and is
**result-identical** to `PypdfPageTextExtractor` for everything a normal
document does (asserted by full-dataclass equality across a corpus,
including budget and over-limit behaviours).

- **One spawn worker per `extract()` call**, `daemon=True`. Spawn costs
  ~0.2 s here, intake handles a handful of documents per session, and
  per-call spawn gives the strongest isolation — no state, memory or CPU
  carryover between untrusted documents — with no pool lifecycle to get
  wrong. A pool would only pay off at volumes this app does not have.
- **Protocol over a one-way pipe**: `meta` (page count, memory-limit
  enforcement), `page_start`, `page` (number, text, status), `done`,
  `fatal` (reason). Text is bounded by `MAX_CHARS_PER_PAGE` as before.
  The PDF bytes go in once as `Process` args; a `PdfReader` is never
  pickled.
- **Parent enforces the wall clock**: the deadline starts *before*
  `proc.start()`; on deadline the parent drains late arrivals, then
  SIGTERMs the worker, joins with a 1.0 s grace, and SIGKILLs if still
  alive. This is the difference between an enforced budget and an
  observed one. The child's own per-page timing only produces the
  `TRUNCATED` (slow-but-completed) marking, exactly as in-process.
- **Partial results survive a timeout**: pages that completed keep their
  number, text and status; the page in flight and pages never attempted
  are `TIMED_OUT` with the same message text the in-process extractor
  uses; pages beyond `max_pages` get the same synthetic
  `SKIPPED_AFTER_LIMIT` entry. Page N's text is never another page's.
- **Memory**: the child applies `resource.setrlimit(RLIMIT_AS)` *before*
  importing pypdf (the module keeps pypdf imports lazy for exactly this
  reason), and reports whether it was enforced. `MemoryError` in the
  child becomes a `fatal` with a bomb-specific reason, not a crash. The
  parent never caps itself.
- **Failure handling**: parse failure, encryption, empty page tree,
  worker death without a final message, spawn failure — each produces an
  `ExtractionResult` with `error` set (the same strings as in-process
  where the paths coincide), so `intake` routes to manual entry. No
  exception escapes; no silent empty success. The stored original PDF is
  only ever read.
- **Diagnostics**: after each call the extractor records
  `last_worker_pid`, `last_worker_exitcode`, `last_worker_terminated`,
  `last_memory_limit_enforced` — so termination is assertable, not
  assumed.

### Platform caveats

- **Linux**: `RLIMIT_AS` enforced (the tests here prove it end to end).
- **macOS**: `setrlimit(RLIMIT_AS)` may succeed while the kernel does not
  enforce it. The worker therefore reports "not enforced" for non-Linux
  platforms rather than claiming a protection it does not have, and the
  result's `document_note` carries a caveat saying a decompression bomb
  may still exhaust memory. pypdf's internal ~72 MiB output cap still
  applies on every platform.
- **Windows**: no `resource` module; same explicit degradation.
- **Inherited limits**: children inherit the parent's rlimits. If the app
  itself runs under a hard `RLIMIT_AS` below the requested worker limit,
  the worker's `setrlimit` fails and the result says so explicitly.

### Judgement calls

- **In-process extractor kept as a documented fallback**, selected with
  `LOCAL_LEDGER_PDF_EXTRACTOR=inprocess` via `default_extractor()`. It is
  fully trustworthy for documents that are not hostile; constrained
  environments (no spawning allowed, debugging) keep a working path. An
  unrecognized value raises rather than silently downgrading safety.
- **No silent in-process fallback on worker failure.** If the worker
  cannot start, the result is an error routed to manual entry — falling
  back invisibly would reintroduce the hang this module exists to prevent.
- **Default budgets** mirror `pdf_text` (500 pages, 5 s/page, 60 s total)
  plus 512 MiB memory and a 1.0 s kill grace, so results and bounds stay
  comparable across the swap.

## The default-extractor seam (needs its owner's one line)

DoD asked for `extract_pages()` to return the worker by default with no
change to files I do not own. `pdf_text.extract_pages` currently hardcodes
`(extractor or PypdfPageTextExtractor())` (pdf_text.py:276), and that file
is read-only for me — its owner is mid-flight in it. Per the prompt's
stop-and-report rule I did not touch it. Everything needed for the flip is
in place and tested; the owner's half is:

```python
def extract_pages(pdf_bytes, *, extractor=None):
    if extractor is None:
        from local_ledger.tax.pdf_worker import default_extractor
        extractor = default_extractor()
    return extractor.extract(pdf_bytes)
```

`test_extract_pages_seam_accepts_the_worker_extractor` proves the swap is
result-neutral at exactly the call `intake` makes, and the env override
gives an escape hatch. I deliberately did *not* patch
`pdf_text.PypdfPageTextExtractor` at import time from my module: an
import-order-dependent monkeypatch is the kind of coupling that looks
merged and fails in production, and nothing imports my module today, so
it would also silently not apply.

## Test evidence (`tests/unit/tax/test_pdf_worker.py`, 12 tests)

| Requirement | Test | Measured |
| --- | --- | --- |
| Never-returning worker terminated within budget | `test_hanging_page_is_terminated_within_the_budget` | 2.0 s budget → caller back in ~2.0 s (bounds 1.5–4.5), exitcode −15, budget message on the timed-out page |
| Termination, not a status flag; no orphans | same + `test_normal_extraction_leaves_no_worker_behind` | `os.kill(pid, 0)` → `ProcessLookupError` after reaping; normal path exits 0 |
| Partial pages correctly attributed | `test_partial_pages_keep_their_attribution_across_a_timeout` | pages 1–2 EXTRACTED with their own markers, 3–4 TIMED_OUT, no cross-page leakage |
| Large-decompression input hits the memory limit with a reason | `test_decompression_bomb_hits_the_os_memory_limit` (64 MiB → `MemoryError` fatal at ~0.2 s); `test_decompression_bomb_hits_pypdf_output_cap_by_default` (page `ERROR` "Limit reached while decompressing") | both < 10 s, parent unaffected |
| Normal multi-page round-trips identically | `test_normal_documents_round_trip_identically` + `test_extract_pages_seam_accepts_the_worker_extractor` | full `ExtractionResult` equality on 8-document corpus incl. uncompressed, extra streams, image-only, mixed, truncated file, `max_pages` overflow, tiny per-page budget |
| Encrypted / corrupt / empty yield error with reason | `test_error_documents_report_reasons_matching_in_process` | equal to in-process results, `pages == ()` |
| Memory-limit degradation explicit | `test_memory_limit_degrades_explicitly`, `test_memory_caveat_assembly` | Linux+success → enforced; refused/`darwin` → not enforced; caveat merges into `document_note` |
| Default factory + escape hatch | `test_default_extractor_factory` | worker default; `inprocess` honored; bad value raises |
| Original never modified | `test_worker_never_touches_the_stored_original` | bytes and mtime unchanged |

Suite at hand-off: **527 passed, 10 skipped** (486 before the other
agent's AX-06 hardening, 515 after it, +12 here). `ruff check src tests`
clean — including the three errors the other agent flagged in my file
mid-flight (unused `noqa`s, a `contextlib.suppress`, a non-raw regex),
all fixed.

One test-design near-miss worth recording: my first version of the
degradation test called the *real* `setrlimit` on the pytest process
(the `darwin` branch runs the syscall before checking the platform).
Children inherit rlimits, so that would have poisoned every later test
and spawned worker. Rewritten with a fake syscall; the runner's real
limits are never touched.

## Found, deliberately not changed

1. **The default flip in `pdf_text.extract_pages`** — its owner's one
   line, quoted above. This is the only remaining step for the worker to
   be what `intake` actually calls by default.
2. **`is_scanned` is `True` when the only page has status `ERROR`** (the
   single-page bomb under pypdf's own cap). Arguably misleading — "we
   could not read it" is not "it is a scan" — but it is the in-process
   extractor's existing semantics (`bool(pages) and not pages_with_text`),
   parity is the stronger requirement here, and `intake` already routes
   both to manual entry. Flagged for `pdf_text.py`'s owner.
3. **`intake.extract_pdf_page_texts` reports whole-document errors on
   "page 1"** via `max(result.page_count, 1)` even for zero-page results
   (intake.py:222). Existing behaviour, harmless for its purpose; noting
   for completeness.
4. **The StrEnum-from-database identity trap** (the other agent hit it
   twice today, e.g. `ReviewScopeMode.SELECTED_ACCOUNTS is False`). My
   worker compares child-reported statuses by
   `PageStatus(msg["status"])` construction precisely to avoid the plain-
   string comparison trap. Their suggestion of a lint rule for
   `StrEnum`-vs-literal identity checks has my support; it is a
   coordinator-level decision, not something I can land inside my file
   ownership.


## Coordinator integration — September 29, 2026

The default flip above is now implemented in `pdf_text.extract_pages()` and
exercised through intake's unchanged call shape. The explicit in-process override
is preserved. `is_scanned` now requires every page to be NO_TEXT_LAYER; parser
errors/timeouts do not establish a scan. Intake classifies all-unreadable results
as unsupported with their per-page reasons. Focused PDF/worker/intake tests:
42 passed. Full unit gate after integration: **533 passed, 10 skipped**; Ruff
clean across src/tests/app/pages. See [integration review](integration-review-2026-09-29.md) for the
separate TAX-06 acceptance findings and full verification evidence.
