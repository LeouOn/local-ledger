"""Worker-process PDF extraction: enforcement, not observation (pdf worker).

These tests pin the behaviours the review found missing from the in-process
extractor, plus result parity so the swap-in changes nothing for normal
documents:

- a page that never finishes is **terminated** within the wall-clock budget
  (asserted against the clock), not merely flagged after the fact;
- no orphaned worker process remains (reaped, dead, non-zero exit);
- pages that completed before the timeout are returned, correctly attributed
  (page N's text is page N's text);
- a decompression bomb fails with a reason at the memory limit, both via the
  OS-level RLIMIT_AS backstop and via pypdf's own output cap;
- encrypted / corrupt / empty input yields ``error`` with a reason, never an
  exception and never a silent empty success;
- a normal multi-page document round-trips identically to
  ``PypdfPageTextExtractor``, including budgets and over-limit reports.

Synthetic documents come from ``tests/unit/tax/pdf_fixtures.py``; the
adversarial ones (slow page, decompression bomb) are built here on the same
primitives.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import zlib
from pathlib import Path

import pytest
from tests.unit.tax import pdf_fixtures as pdf

from local_ledger.tax.pdf_text import (
    PageStatus,
    PypdfPageTextExtractor,
    extract_pages,
)
from local_ledger.tax.pdf_worker import (
    WorkerPageTextExtractor,
    _apply_memory_limit,
    default_extractor,
)

# 400k trivial text-showing operators take ~14 s of pypdf parsing on this
# class of machine (measured; see the work log). The tests give the worker a
# 2 s budget, so the kill is time-based and stays deterministic with a ~7x
# margin even on faster hardware.
SLOW_PAGE_OPS = 400_000
WALL_BUDGET = 2.0


def slow_page_pdf(fast_texts: list[str], ops: int, tail_pages: int = 0) -> bytes:
    """Text pages, then one CPU-heavy page, then optional never-reached pages."""
    n_fast = len(fast_texts)
    total = n_fast + 1 + tail_pages
    kids = [3 + i for i in range(total)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{k} 0 R".encode() for k in kids)
            + b"] /Count %d >>" % total
        ),
        900: pdf.FONT_OBJECT,
    }
    for i, text in enumerate(fast_texts):
        payload = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET\n".encode()
        objects[700 + i] = (
            b"<< /Length %d >>\nstream\n" % len(payload) + payload + b"endstream"
        )
        objects[kids[i]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 900 0 R >> >> /Contents %d 0 R >>" % (700 + i)
        )
    heavy = b"BT /F1 12 Tf 20 100 Td (AAAAAAAAAA) Tj ET\n" * ops
    comp = zlib.compress(heavy)
    heavy_num = 700 + n_fast
    objects[heavy_num] = (
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(comp)
        + comp
        + b"\nendstream"
    )
    objects[kids[n_fast]] = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 900 0 R >> >> /Contents %d 0 R >>" % heavy_num
    )
    for j in range(tail_pages):
        payload = b"BT /F1 12 Tf 20 100 Td (TAIL PAGE) Tj ET\n"
        num = 760 + j
        objects[num] = (
            b"<< /Length %d >>\nstream\n" % len(payload) + payload + b"endstream"
        )
        objects[kids[n_fast + 1 + j]] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 900 0 R >> >> /Contents %d 0 R >>" % num
        )
    return pdf.assemble_pdf(objects)


def decompression_bomb_pdf(decompressed_mb: int) -> bytes:
    """A small file whose single content stream expands to ``decompressed_mb``.

    The payload is compressed in ~8 MiB chunks so building the fixture never
    allocates the decompressed size in the test process.
    """
    comp = zlib.compressobj()
    chunk = b"(Aaaaaaaaaaaaaaaaaaaaaaaa) Tj " * 262144  # ~8 MiB
    out = bytearray()
    for _ in range(decompressed_mb // 8):
        out += comp.compress(chunk)
    out += comp.flush()
    data = bytes(out)
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        900: pdf.FONT_OBJECT,
        700: (
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data)
            + data
            + b"\nendstream"
        ),
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 900 0 R >> >> /Contents 700 0 R >>"
        ),
    }
    return pdf.assemble_pdf(objects)


@pytest.fixture(scope="module")
def bomb_pdf() -> bytes:
    return decompression_bomb_pdf(512)


def _assert_process_is_gone(pid: int | None) -> None:
    """Real termination evidence: reaped (not zombie) and not schedulable."""
    assert pid is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return  # the process is gone
    pytest.fail(f"worker pid {pid} still exists after the budget was enforced")


# ------------------------------------------------------- parity with in-process


def test_normal_documents_round_trip_identically() -> None:
    """The swap-in changes no result for everything a real document does."""
    noise = {
        500: b"<< /Length 30 >>\nstream\nsome metadata bytes here\nendstream",
        501: b"<< /Length 26 >>\nstream\nanother unrelated stream\nendstream",
    }
    truncated = pdf.text_pdf(["PAGE ONE DATA", "PAGE TWO DATA"])
    corpus: list[tuple[bytes, PypdfPageTextExtractor]] = [
        (
            pdf.text_pdf(
                [
                    "PAGE ONE ACME 1099-B box 1a $3,850.00",
                    "PAGE TWO LOT DETAIL acquired 03/15/2024",
                    "PAGE THREE FORM 1098 interest $12,450.00",
                ]
            ),
            PypdfPageTextExtractor(),
        ),
        (pdf.text_pdf(["FIRST PAGE TEXT", "SECOND PAGE TEXT"], compress=False),
         PypdfPageTextExtractor()),
        (pdf.text_pdf(["ALPHA PAGE", "BRAVO PAGE", "CHARLIE PAGE"], extra_objects=noise),
         PypdfPageTextExtractor()),
        (pdf.image_only_pdf(page_count=2), PypdfPageTextExtractor()),
        (pdf.mixed_pdf("COVER SUMMARY"), PypdfPageTextExtractor()),
        (truncated[: len(truncated) // 2], PypdfPageTextExtractor()),
        # Page-count overflow report is identical.
        (pdf.text_pdf([f"PAGE {i}" for i in range(1, 7)]), PypdfPageTextExtractor(max_pages=3)),
        # Tiny per-page budget: slow-but-completing pages report TRUNCATED.
        (
            pdf.text_pdf(["PAGE ONE DATA", "PAGE TWO DATA"]),
            PypdfPageTextExtractor(per_page_seconds=0.000001, max_pages=2),
        ),
    ]
    for doc, in_process in corpus:
        worker = WorkerPageTextExtractor(
            max_pages=in_process.max_pages,
            per_page_seconds=in_process.per_page_seconds,
            total_seconds=in_process.total_seconds,
        )
        expected = in_process.extract(doc)
        actual = worker.extract(doc)
        assert actual == expected, (
            f"worker result differs for a normal document:\n"
            f"in-process: {expected}\nworker:     {actual}"
        )
        # Worker finished on its own: no termination, clean exit.
        assert worker.last_worker_terminated is False
        assert worker.last_worker_exitcode == 0


def test_error_documents_report_reasons_matching_in_process() -> None:
    encrypted = pdf.encrypted_pdf()  # built once: both extractors see equal bytes
    corpus = [
        encrypted,
        b"%PDF-1.4\nthis is not a valid pdf body",
        b"",
    ]
    for doc in corpus:
        expected = PypdfPageTextExtractor().extract(doc)
        actual = WorkerPageTextExtractor().extract(doc)
        assert actual == expected
        assert actual.error is not None
        assert actual.pages == ()


def test_extract_pages_seam_accepts_the_worker_extractor() -> None:
    """The exact call intake makes, with the worker swapped in, is identical."""
    doc = pdf.text_pdf(["INTAKE SEAM PAGE ONE 100.00", "INTAKE SEAM PAGE TWO 200.00"])
    via_default = extract_pages(doc)
    via_worker = extract_pages(doc, extractor=WorkerPageTextExtractor())
    assert via_worker == via_default


# ------------------------------------------------------------- enforcement


def test_hanging_page_is_terminated_within_the_budget() -> None:
    doc = slow_page_pdf(["BEFORE HEAVY PAGE"], SLOW_PAGE_OPS)
    extractor = WorkerPageTextExtractor(total_seconds=WALL_BUDGET)

    started = time.monotonic()
    result = extractor.extract(doc)
    elapsed = time.monotonic() - started

    # The budget is enforced against the wall clock: back roughly on time,
    # and nowhere near the ~14 s the page needs if left alone.
    assert 1.5 <= elapsed <= 4.5, f"caller waited {elapsed:.2f}s for a {WALL_BUDGET}s budget"

    # Termination actually happened, not just a status flag.
    assert extractor.last_worker_terminated is True
    assert extractor.last_worker_exitcode is not None
    assert extractor.last_worker_exitcode != 0
    assert extractor.last_worker_exitcode in (-signal.SIGTERM, -signal.SIGKILL)
    _assert_process_is_gone(extractor.last_worker_pid)

    # The page that finished is returned with its own text; the rest are
    # budget-exhausted, never silently dropped or misattributed.
    assert result.page_count == 2
    assert result.pages[0].status is PageStatus.EXTRACTED
    assert result.pages[0].text.strip() == "BEFORE HEAVY PAGE"
    assert result.pages[1].status is PageStatus.TIMED_OUT
    assert "budget of 2s was reached" in result.pages[1].text
    assert result.truncated is True
    assert result.error is None
    # A partial result is not "scanned" and is not an error: the reviewer
    # gets the one page that succeeded plus manual entry for the rest.
    assert result.is_scanned is False


def test_partial_pages_keep_their_attribution_across_a_timeout() -> None:
    doc = slow_page_pdf(["FIRST PAGE MARKER", "SECOND PAGE MARKER"], SLOW_PAGE_OPS, tail_pages=1)
    extractor = WorkerPageTextExtractor(total_seconds=WALL_BUDGET)
    result = extractor.extract(doc)

    assert result.page_count == 4
    p1, p2, p3, p4 = result.pages
    assert p1.status is PageStatus.EXTRACTED
    assert p1.text.strip() == "FIRST PAGE MARKER"
    assert p2.status is PageStatus.EXTRACTED
    assert p2.text.strip() == "SECOND PAGE MARKER"
    # Page N is page N: completed pages never leak into each other.
    assert "FIRST PAGE MARKER" not in p2.text
    assert "SECOND PAGE MARKER" not in p1.text
    # The page in flight and the page never attempted are both budget-exhausted.
    assert p3.status is PageStatus.TIMED_OUT
    assert p4.status is PageStatus.TIMED_OUT
    assert "FIRST PAGE MARKER" not in p4.text
    assert result.truncated is True


# ------------------------------------------------------------- memory limits


def test_decompression_bomb_hits_the_os_memory_limit(bomb_pdf: bytes) -> None:
    extractor = WorkerPageTextExtractor(memory_limit_mb=64, total_seconds=30)
    started = time.monotonic()
    result = extractor.extract(bomb_pdf)
    elapsed = time.monotonic() - started

    assert result.error is not None
    assert "memory limit" in result.error
    assert "decompression bomb" in result.error
    assert result.pages == ()
    assert elapsed < 10, "the bomb must fail fast, not grind"
    # The worker reported its own death cleanly; the parent survived.
    assert extractor.last_worker_terminated is False
    assert extractor.last_worker_exitcode == 0
    _assert_process_is_gone(extractor.last_worker_pid)


def test_decompression_bomb_hits_pypdf_output_cap_by_default(bomb_pdf: bytes) -> None:
    """Under the default limit, pypdf's own ~75 MB cap fires first.

    The worker must surface that as a per-page reason, not crash or hang.
    """
    extractor = WorkerPageTextExtractor()
    result = extractor.extract(bomb_pdf)

    assert result.error is None
    assert result.page_count == 1
    assert result.pages[0].status is PageStatus.ERROR
    assert "Limit reached while decompressing" in result.pages[0].text
    assert extractor.last_worker_terminated is False
    assert extractor.last_worker_exitcode == 0


def test_memory_limit_degrades_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platforms that cannot enforce RLIMIT_AS say so instead of lying.

    The real setrlimit is never called here: applying an address-space limit
    to the test runner itself would poison every later test (and every
    spawned child, which inherits it). A fake stands in for the syscall.
    """
    import resource

    calls: list[tuple[int, int]] = []

    def _fake_setrlimit(kind: int, limits: tuple[int, int]) -> None:
        calls.append(limits)

    def _refuse(_kind: int, _limits: tuple[int, int]) -> None:
        raise ValueError("setrlimit not supported here")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(resource, "setrlimit", _fake_setrlimit)
    assert _apply_memory_limit(128) is True  # Linux enforces the limit
    assert calls and calls[-1] == (128 * 1024 * 1024, 128 * 1024 * 1024)

    # A platform that refuses the call degrades to "not enforced".
    monkeypatch.setattr(resource, "setrlimit", _refuse)
    assert _apply_memory_limit(128) is False

    # macOS accepts the call but does not enforce it; still not "enforced".
    monkeypatch.setattr(resource, "setrlimit", _fake_setrlimit)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _apply_memory_limit(128) is False


def test_memory_caveat_assembly() -> None:
    """The caveat text exists only for enforced=False, and merges cleanly."""
    from local_ledger.tax.pdf_worker import _caveat_for, _final_note

    caveat = _caveat_for(False)
    assert "could not be enforced" in caveat
    assert _caveat_for(True) == ""
    assert _caveat_for(None) == ""
    # Merged after the empty-page note, or alone when there is no other note.
    assert _final_note("Pages with no extractable text: 2.", False).endswith(caveat)
    assert _final_note("", False) == caveat
    assert _final_note("Pages with no extractable text: 2.", True) == "Pages with no extractable text: 2."
    assert _final_note("", None) == ""
    # End-to-end the caveat only appears on platforms where the worker child
    # cannot enforce RLIMIT_AS (macOS, Windows, or an inherited hard limit
    # below the request); on Linux the child always enforces it, which the
    # parity tests rely on (their notes carry no caveat).


# ------------------------------------------------------------- default factory


def test_default_extractor_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(default_extractor(), WorkerPageTextExtractor)

    monkeypatch.setenv("LOCAL_LEDGER_PDF_EXTRACTOR", "inprocess")
    assert isinstance(default_extractor(), PypdfPageTextExtractor)

    monkeypatch.setenv("LOCAL_LEDGER_PDF_EXTRACTOR", "cloud")
    with pytest.raises(ValueError, match=r"worker.*inprocess|LOCAL_LEDGER_PDF_EXTRACTOR"):
        default_extractor()


def test_normal_extraction_leaves_no_worker_behind() -> None:
    extractor = WorkerPageTextExtractor()
    result = extractor.extract(pdf.text_pdf(["ONE", "TWO"]))
    assert result.page_count == 2
    assert extractor.last_worker_terminated is False
    assert extractor.last_worker_exitcode == 0
    _assert_process_is_gone(extractor.last_worker_pid)


def test_worker_never_touches_the_stored_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction reads bytes; the stored original is never modified."""
    doc = pdf.text_pdf(["STORED ORIGINAL PAGE"])
    original = tmp_path / "original.pdf"
    original.write_bytes(doc)
    before = original.stat().st_mtime_ns

    result = WorkerPageTextExtractor().extract(original.read_bytes())

    assert result.pages[0].status is PageStatus.EXTRACTED
    assert original.read_bytes() == doc
    assert original.stat().st_mtime_ns == before
