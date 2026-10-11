"""Interruptible PDF text extraction in a worker process (pdf worker).

Why this module exists
----------------------

:class:`local_ledger.tax.pdf_text.PypdfPageTextExtractor` enforces time and
size budgets *observationally*: it measures elapsed time only after
``extract_text()`` returns, and the initial parse/page-tree traversal happens
outside the measured budget entirely. A document that hangs inside the parser
therefore blocks intake indefinitely, no matter what budget was configured.
An in-process library call cannot be interrupted; switching PDF libraries
would not change that. The fix is a worker process whose wall-clock budget is
enforced by killing it.

What this module provides
-------------------------

:class:`WorkerPageTextExtractor` implements the same
:class:`local_ledger.tax.pdf_text.PageTextExtractor` protocol, returns the
same :class:`~local_ledger.tax.pdf_text.ExtractionResult` shapes, and is
result-identical to the in-process extractor for every input the in-process
extractor handles (the tests assert full equality on a corpus of normal,
mixed, image-only, encrypted, corrupt, truncated and over-limit documents).

Guarantees, in the order the review stated them:

1. **Real process isolation.** Parsing and ``extract_text`` run in a spawn
   context child. A hang is terminated (SIGTERM, then SIGKILL after a grace
   period), not observed after the fact.
2. **Memory limit** via ``resource.setrlimit(RLIMIT_AS)`` applied in the
   child *before* pypdf is imported. Linux enforces RLIMIT_AS; macOS accepts
   but does not enforce it and Windows lacks the ``resource`` module, so the
   limit degrades explicitly there and the result says so (see
   ``_apply_memory_limit``). pypdf additionally caps decompression output at
   ~75 MB by default (``LimitReachedError``); the OS limit is the backstop
   for every allocation path pypdf's cap does not cover.
3. **Per-page granularity preserved.** The worker streams one result per
   page; pages that completed before a timeout are returned with their own
   text, the page in flight is ``TIMED_OUT``, pages never attempted are
   ``TIMED_OUT`` (budget-exhausted, matching the in-process semantics), and
   pages beyond ``max_pages`` get the same synthetic
   ``SKIPPED_AFTER_LIMIT`` entry the in-process extractor produces.
4. **The caller never hangs.** The wall-clock budget starts before the
   worker is spawned and is enforced by termination, so a 2-second budget
   returns in roughly 2 seconds plus kill latency.
5. **Bounded pickling.** The PDF bytes go in once; only small structured
   messages (page status, text capped at ``MAX_CHARS_PER_PAGE``) come back.
   A ``PdfReader`` is never pickled.
6. **Failures are information.** Worker death, memory exhaustion, timeout
   and malformed input each produce an ``ExtractionResult`` with a reason;
   no exception escapes to the caller, and there is no silent empty success.
   A failed extraction still routes to manual field entry, exactly as the
   in-process path does.

Storage safety: extraction never touches the stored original PDF; it only
reads the bytes it is handed.

Usage and the default-extractor seam
------------------------------------

``intake.py`` calls ``pdf_text.extract_pages(pdf_bytes)``. This module does
not modify that seam; it provides the drop-in implementation::

    from local_ledger.tax.pdf_worker import default_extractor
    result = pdf_text.extract_pages(pdf_bytes, extractor=default_extractor())

``default_extractor()`` honours ``LOCAL_LEDGER_PDF_EXTRACTOR``:

- ``worker`` (default): :class:`WorkerPageTextExtractor`
- ``inprocess``: the original :class:`PypdfPageTextExtractor`, for
  constrained environments where spawning is unwanted; it remains fully
  trustworthy for documents that are not hostile.

Making the worker the process-wide default is a one-line change in
``pdf_text.extract_pages`` (its owner's file); see the work log for the
exact patch. This module deliberately does not patch that default at import
time - an import-order-dependent monkeypatch is exactly the kind of silent
coupling that looks merged and fails in production.

One worker is spawned per ``extract()`` call rather than pooled: spawn costs
~0.2 s here, intake handles a handful of documents per session, and per-call
spawnion gives the strongest isolation (no state, memory or CPU carryover
between untrusted documents) with no lifecycle code to get wrong.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time
from dataclasses import dataclass

#: Defaults mirror pdf_text so results and bounds stay comparable.
DEFAULT_MEMORY_LIMIT_MB = 512
KILL_GRACE_SECONDS = 1.0


def _pdf_text():  # type: ignore[no-untyped-def]
    """Import pdf_text lazily.

    Two reasons: the spawn child must apply RLIMIT_AS *before* pypdf is
    imported (module-level imports here run in the child too), and the
    parent should not pay for the import chain when only constructing the
    extractor.
    """
    from local_ledger.tax import pdf_text

    return pdf_text


def _apply_memory_limit(limit_mb: int) -> bool:
    """Apply RLIMIT_AS in the current process. True iff actually enforced.

    Linux enforces the limit. macOS documents RLIMIT_AS as not enforced
    (setrlimit may succeed while doing nothing), so it is reported as not
    enforced rather than lying. Windows has no ``resource`` module.

    Only the worker child calls this; the parent never caps itself. Note
    that children inherit the parent's rlimits, so a parent already running
    under a lower hard limit will make the worker's setrlimit fail - that
    degrades explicitly via the result's document note.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return False
    if not hasattr(resource, "RLIMIT_AS"):  # pragma: no cover - exotic
        return False
    limit_bytes = int(limit_mb) * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ValueError, OSError):
        # Raised when the platform refuses the call (or the inherited hard
        # limit is already below the request); degrade explicitly.
        return False
    return sys.platform.startswith(("linux", "freebsd"))


def _caveat_for(enforced: bool | None) -> str:
    """The document-note caveat when the worker's memory limit was not enforced."""
    if enforced is False:
        return (
            "Worker memory limit could not be enforced on this platform; "
            "a decompression bomb may still exhaust memory."
        )
    return ""


def _final_note(note: str, enforced: bool | None) -> str:
    """Merge the empty-page note and the memory caveat into one document note.

    Pure so the assembly can be tested on any platform: the ``enforced``
    flag itself originates in the worker child, which on Linux always
    enforces the limit.
    """
    caveat = _caveat_for(enforced)
    if not caveat:
        return note
    return f"{note} {caveat}".strip()


@dataclass(frozen=True)
class _WorkerParams:
    """Everything the child needs; plain data so it pickles cheaply."""

    max_pages: int
    per_page_seconds: float
    total_seconds: float
    layout: bool
    memory_limit_mb: int


def _child_extract(conn, pdf_bytes: bytes, params: _WorkerParams) -> None:  # type: ignore[no-untyped-def]
    """Child entry point: stream per-page results over the pipe.

    Runs entirely in the worker process. Memory limit first, then imports.
    Never raises: every failure becomes a ``fatal`` message so the parent
    can build a reasoned result. If this function dies anyway, the parent
    notices (no ``done``/``fatal`` and the process exited) and reports that.
    """
    enforced = _apply_memory_limit(params.memory_limit_mb)
    try:
        import io

        pdf_text = _pdf_text()
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        except Exception as exc:  # malformed xref, bad object, truncated file
            _send_fatal(conn, f"could not parse PDF: {exc}", enforced)
            return

        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    _send_fatal(
                        conn,
                        "PDF is encrypted; open it and enter fields manually",
                        enforced,
                    )
                    return
            except Exception as exc:
                _send_fatal(
                    conn, f"PDF is encrypted and could not be opened: {exc}", enforced
                )
                return

        try:
            total_pages = len(reader.pages)
        except Exception as exc:
            _send_fatal(conn, f"could not read the page tree: {exc}", enforced)
            return

        if total_pages == 0:
            _send_fatal(conn, "PDF contains no pages", enforced)
            return

        conn.send({"kind": "meta", "page_count": total_pages, "enforced": enforced})

        max_chars = pdf_text.MAX_CHARS_PER_PAGE
        for index in range(min(total_pages, params.max_pages)):
            page_number = index + 1
            conn.send({"kind": "page_start", "n": page_number})
            page_started = time.monotonic()
            try:
                page = reader.pages[index]
                raw = page.extract_text(
                    extraction_mode="layout" if params.layout else "plain"
                )
            except Exception as exc:
                conn.send(
                    {
                        "kind": "page",
                        "n": page_number,
                        "text": (
                            f"(Page {page_number} could not be read: {exc}. "
                            "Enter fields manually.)"
                        ),
                        "status": "ERROR",
                    }
                )
                continue

            elapsed = time.monotonic() - page_started
            text = (raw or "").strip()
            if not text:
                conn.send(
                    {
                        "kind": "page",
                        "n": page_number,
                        "text": (
                            f"(Page {page_number} has no extractable text - it may be "
                            "an image or scan. pypdf does not perform OCR; enter "
                            "fields manually.)"
                        ),
                        "status": "NO_TEXT_LAYER",
                    }
                )
                continue

            if len(text) > max_chars or elapsed > params.per_page_seconds:
                conn.send(
                    {
                        "kind": "page",
                        "n": page_number,
                        "text": text[:max_chars],
                        "status": "TRUNCATED",
                    }
                )
                continue

            conn.send({"kind": "page", "n": page_number, "text": text, "status": "EXTRACTED"})

        conn.send({"kind": "done"})
    except MemoryError:
        _send_fatal(
            conn,
            f"worker exceeded its {params.memory_limit_mb} MiB memory limit while "
            "reading the PDF; the document may be a decompression bomb. "
            "Enter fields manually.",
            enforced,
        )
    except BaseException as exc:  # the parent must never see a crash
        _send_fatal(conn, f"worker failed unexpectedly: {type(exc).__name__}: {exc}", enforced)
    finally:
        conn.close()


def _send_fatal(conn, reason: str, enforced: bool) -> None:  # type: ignore[no-untyped-def]
    """Best-effort fatal report; if even this fails, the parent sees a dead worker."""
    import contextlib

    with contextlib.suppress(Exception):  # pipe already broken
        conn.send({"kind": "fatal", "reason": reason, "enforced": enforced})


class WorkerPageTextExtractor:
    """Worker-process implementation of the PageTextExtractor protocol.

    Same constructor bounds as ``PypdfPageTextExtractor`` (so swapping
    changes no caller), plus worker-specific knobs. Diagnostics from the
    last ``extract()`` are exposed as attributes for tests and for anyone
    who needs to prove termination really happened.
    """

    name = "pypdf-worker"

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        per_page_seconds: float | None = None,
        total_seconds: float | None = None,
        layout: bool = False,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
    ) -> None:
        pdf_text = _pdf_text()
        self.max_pages = pdf_text.MAX_PAGES if max_pages is None else max_pages
        self.per_page_seconds = (
            pdf_text.PER_PAGE_SECONDS if per_page_seconds is None else per_page_seconds
        )
        self.total_seconds = (
            pdf_text.TOTAL_SECONDS if total_seconds is None else total_seconds
        )
        self.layout = layout
        self.memory_limit_mb = memory_limit_mb
        self.kill_grace_seconds = kill_grace_seconds

        # Diagnostics from the most recent extract() call.
        self.last_worker_pid: int | None = None
        self.last_worker_exitcode: int | None = None
        self.last_worker_terminated: bool = False
        self.last_memory_limit_enforced: bool | None = None

    def extract(self, pdf_bytes: bytes):  # type: ignore[no-untyped-def]
        pdf_text = _pdf_text()
        PageStatus = pdf_text.PageStatus
        PageText = pdf_text.PageText
        ExtractionResult = pdf_text.ExtractionResult

        params = _WorkerParams(
            max_pages=self.max_pages,
            per_page_seconds=self.per_page_seconds,
            total_seconds=self.total_seconds,
            layout=self.layout,
            memory_limit_mb=self.memory_limit_mb,
        )

        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        started = time.monotonic()
        deadline = started + self.total_seconds
        try:
            proc = ctx.Process(
                target=_child_extract, args=(child_conn, bytes(pdf_bytes), params)
            )
            proc.daemon = True
            proc.start()
        except Exception as exc:
            child_conn.close()
            parent_conn.close()
            return ExtractionResult(
                pages=(),
                page_count=0,
                error=f"could not start the extraction worker: {exc}",
            )
        # The parent never uses the child end of the pipe.
        child_conn.close()

        self.last_worker_pid = proc.pid
        self.last_worker_terminated = False

        page_count: int | None = None
        enforced: bool | None = None
        received: dict[int, dict[str, str]] = {}
        finished = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not parent_conn.poll(remaining):
                break  # budget exhausted
            try:
                msg = parent_conn.recv()
            except (EOFError, OSError):
                break  # worker died or reset the pipe; diagnosed below
            kind = msg.get("kind")
            if kind == "meta":
                page_count = int(msg["page_count"])
                enforced = bool(msg["enforced"])
            elif kind == "page":
                received[int(msg["n"])] = msg
            elif kind == "page_start" or kind == "done":
                if kind == "done":
                    finished = True
                    break
            elif kind == "fatal":
                reason = str(msg["reason"])
                enforced = bool(msg.get("enforced", enforced))
                proc.join(2.0)
                self.last_worker_exitcode = proc.exitcode
                self.last_memory_limit_enforced = enforced
                parent_conn.close()
                return ExtractionResult(
                    pages=(),
                    page_count=page_count or 0,
                    error=reason,
                )

        # Drain anything the worker managed to send before the deadline.
        while parent_conn.poll(0):
            try:
                msg = parent_conn.recv()
            except (EOFError, OSError):
                break
            if msg.get("kind") == "page":
                received.setdefault(int(msg["n"]), msg)

        if not finished:
            # Enforce the wall-clock budget by killing the worker. This is
            # the difference between an observed limit and an enforced one.
            proc.terminate()
            proc.join(self.kill_grace_seconds)
            if proc.is_alive():
                proc.kill()
                proc.join()
            self.last_worker_terminated = True
        else:
            proc.join(2.0)
        self.last_worker_exitcode = proc.exitcode
        self.last_memory_limit_enforced = enforced
        parent_conn.close()

        if not finished and page_count is None:
            # Not even the page tree arrived: nothing was read at all.
            return ExtractionResult(
                pages=(),
                page_count=0,
                error=(
                    f"extraction did not finish within the "
                    f"{self.total_seconds:.0f}s budget; no pages were read. "
                    "Enter fields manually."
                ),
            )

        pages: list[PageText] = []
        truncated = False
        assert page_count is not None
        limit_hit = page_count > self.max_pages

        for page_number in range(1, min(page_count, self.max_pages) + 1):
            msg = received.get(page_number)
            if msg is not None:
                status = PageStatus(msg["status"])
                if status is PageStatus.TRUNCATED:
                    truncated = True
                pages.append(
                    PageText(page_number=page_number, text=msg["text"], status=status)
                )
            else:
                # Page in flight when the budget expired, or never attempted:
                # both are budget-exhausted, exactly as the in-process
                # extractor marks them.
                truncated = True
                pages.append(
                    PageText(
                        page_number=page_number,
                        text=(
                            f"(Page {page_number} was not read: extraction time "
                            f"budget of {self.total_seconds:.0f}s was reached. "
                            "Enter fields manually.)"
                        ),
                        status=PageStatus.TIMED_OUT,
                    )
                )

        if limit_hit:
            truncated = True
            pages.append(
                PageText(
                    page_number=min(page_count, self.max_pages) + 1,
                    text=(
                        f"(Document has {page_count} pages; only the first "
                        f"{self.max_pages} were read. Enter fields from the "
                        "remaining pages manually.)"
                    ),
                    status=PageStatus.SKIPPED_AFTER_LIMIT,
                )
            )

        note = ""
        empty = [p.page_number for p in pages if not p.has_text]
        if empty and len(empty) < len(pages):
            note = (
                f"Pages with no extractable text: {', '.join(str(n) for n in empty)}. "
                "This is a mixed document; those pages need manual entry."
            )
        note = _final_note(note, enforced)

        return ExtractionResult(
            pages=tuple(pages),
            page_count=page_count,
            document_note=note,
            truncated=truncated,
        )


def default_extractor():  # type: ignore[no-untyped-def]
    """The extractor ``extract_pages`` should use by default.

    ``LOCAL_LEDGER_PDF_EXTRACTOR=worker`` (or unset) selects the worker;
    ``inprocess`` selects the original pypdf extractor for environments
    where spawning a process is unwanted. Anything else is a configuration
    error and fails loudly rather than silently downgrading safety.
    """
    pdf_text = _pdf_text()
    choice = os.environ.get("LOCAL_LEDGER_PDF_EXTRACTOR", "worker").strip().lower()
    if choice == "worker":
        return WorkerPageTextExtractor()
    if choice == "inprocess":
        return pdf_text.PypdfPageTextExtractor()
    raise ValueError(
        f"LOCAL_LEDGER_PDF_EXTRACTOR must be 'worker' or 'inprocess', got {choice!r}"
    )
