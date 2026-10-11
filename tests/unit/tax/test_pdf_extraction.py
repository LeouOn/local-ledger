"""Per-page PDF extraction: real synthetic PDFs, not a mock.

The behaviours pinned here are the ones a wrong page reference would quietly
break, and the ones a hand-rolled PDF parser gets wrong:

- page N's text is page N's text, even when the file stores objects out of order
  or carries more streams than pages (fonts, images, metadata);
- a page with no text says so *on that page* and never shows another page's
  content - this is the regression that motivated pypdf;
- a mixed text/image document is not reported as scanned;
- image-only, encrypted and corrupt input are reported with a reason and routed
  to manual entry, never as a silent empty success;
- the stored original is still there for visual verification;
- page count and time budgets are bounded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.tax import pdf_fixtures as pdf

from local_ledger.tax.pdf_text import (
    MAX_PAGES,
    ExtractionResult,
    PageStatus,
    PageText,
    PypdfPageTextExtractor,
    extract_pages,
    preview_for_page,
)


@pytest.mark.parametrize("status", [PageStatus.ERROR, PageStatus.TIMED_OUT, PageStatus.SKIPPED_AFTER_LIMIT])
def test_unreadable_pages_do_not_establish_a_scan(status: PageStatus) -> None:
    result = ExtractionResult(
        pages=(PageText(page_number=1, text="not read", status=status),), page_count=1
    )
    assert result.is_scanned is False


def test_intake_reports_unreadable_pages_as_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    from local_ledger.tax import intake
    from local_ledger.tax.models import ScanState

    result = ExtractionResult(
        pages=(PageText(page_number=1, text="parser failed", status=PageStatus.ERROR),),
        page_count=1,
    )
    monkeypatch.setattr(intake, "extract_pages", lambda _: result)
    state, count, pages = intake.extract_pdf_page_texts(b"%PDF-synthetic")
    assert state == ScanState.UNSUPPORTED_TYPE
    assert count == 1
    assert pages[1] == "parser failed"


def test_extract_pages_calls_the_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from local_ledger.tax import pdf_worker

    monkeypatch.delenv("LOCAL_LEDGER_PDF_EXTRACTOR", raising=False)
    calls: list[bytes] = []
    result = ExtractionResult(pages=(), page_count=0, error="worker unavailable")

    class FailedWorker:
        def extract(self, pdf_bytes: bytes) -> ExtractionResult:
            calls.append(pdf_bytes)
            return result

    monkeypatch.setattr(pdf_worker, "default_extractor", FailedWorker)
    assert extract_pages(b"synthetic") is result
    assert calls == [b"synthetic"]


def test_extract_pages_honors_inprocess_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LEDGER_PDF_EXTRACTOR", "inprocess")
    document = pdf.text_pdf(["EXPLICIT INPROCESS"])
    assert extract_pages(document) == PypdfPageTextExtractor().extract(document)

# The synthetic PDFs come from tests/unit/tax/pdf_fixtures.py so the intake
# tests and these tests exercise the same real documents.


# ---------------------------------------------------------------- page attribution


def test_each_page_reports_its_own_text() -> None:
    result = PypdfPageTextExtractor().extract(
        pdf.text_pdf(
            [
                "PAGE ONE ACME 1099-B box 1a $3,850.00",
                "PAGE TWO LOT DETAIL acquired 03/15/2024",
                "PAGE THREE FORM 1098 interest $12,450.00",
            ]
        )
    )
    assert result.page_count == 3
    assert "3,850.00" in result.pages[0].text
    assert "acquired" in result.pages[1].text
    assert "12,450.00" in result.pages[2].text
    # The regression this module exists for: no page shows another page's text.
    assert "3,850.00" not in result.pages[1].text
    assert "3,850.00" not in result.pages[2].text
    assert [p.page_number for p in result.pages] == [1, 2, 3]


def test_page_attribution_survives_uncompressed_documents() -> None:
    result = PypdfPageTextExtractor().extract(
        pdf.text_pdf(["FIRST PAGE TEXT", "SECOND PAGE TEXT"], compress=False)
    )
    assert result.pages[0].text.strip() == "FIRST PAGE TEXT"
    assert result.pages[1].text.strip() == "SECOND PAGE TEXT"


def test_page_attribution_survives_extra_streams_in_the_file() -> None:
    """Fonts and metadata add streams; page N must still be page N.

    The old parser indexed content streams positionally, so a document with an
    extra stream shifted every later page's text.
    """
    noise = {
        500: b"<< /Length 30 >>\nstream\nsome metadata bytes here\nendstream",
        501: b"<< /Length 26 >>\nstream\nanother unrelated stream\nendstream",
    }
    result = PypdfPageTextExtractor().extract(
        pdf.text_pdf(["ALPHA PAGE", "BRAVO PAGE", "CHARLIE PAGE"], extra_objects=noise)
    )
    assert result.page_count == 3
    assert result.pages[0].text.strip() == "ALPHA PAGE"
    assert result.pages[1].text.strip() == "BRAVO PAGE"
    assert result.pages[2].text.strip() == "CHARLIE PAGE"


def test_a_page_with_no_text_never_shows_the_whole_document() -> None:
    """The specific defect: a page that failed to parse was filled with total_text."""
    result = PypdfPageTextExtractor().extract(pdf.image_only_pdf(page_count=2))
    assert result.page_count == 2
    for page in result.pages:
        assert page.status is PageStatus.NO_TEXT_LAYER
        assert "no extractable text" in page.text
    assert result.is_scanned is True


def test_page_count_matches_the_page_tree_not_a_regex() -> None:
    result = PypdfPageTextExtractor().extract(pdf.text_pdf(["ONE", "TWO", "THREE", "FOUR", "FIVE"]))
    assert result.page_count == 5
    assert len(result.pages) == 5


# ---------------------------------------------------------------- mixed documents


def test_a_mixed_document_is_not_reported_as_scanned() -> None:
    """A text cover page with an image-only statement page is not a scan."""
    text_only = PypdfPageTextExtractor().extract(pdf.text_pdf(["COVER PAGE SUMMARY"]))
    assert text_only.is_scanned is False

    result = PypdfPageTextExtractor().extract(pdf.mixed_pdf("COVER SUMMARY"))
    assert result.page_count == 2
    assert result.is_scanned is False, "one readable page is enough to not be a scan"
    assert result.pages[0].status is PageStatus.EXTRACTED
    assert result.pages[1].status is PageStatus.NO_TEXT_LAYER
    assert "2" in result.document_note
    assert "COVER SUMMARY" not in result.pages[1].text


# ---------------------------------------------------------------- unreadable input


def test_an_encrypted_document_reports_a_reason_not_an_empty_success() -> None:
    result = PypdfPageTextExtractor().extract(pdf.encrypted_pdf())
    assert result.error is not None
    assert "encrypt" in result.error.lower()
    assert result.pages == ()


def test_a_corrupt_document_reports_a_reason() -> None:
    result = PypdfPageTextExtractor().extract(b"%PDF-1.4\nthis is not a valid pdf body")
    assert result.error is not None
    assert result.pages == ()


def test_a_truncated_document_does_not_crash() -> None:
    full = pdf.text_pdf(["PAGE ONE DATA", "PAGE TWO DATA"])
    result = PypdfPageTextExtractor().extract(full[: len(full) // 2])
    # Either a clean error or a partial result: never an exception, and never a
    # page claiming text it does not have.
    assert result.pages == () or all(p.status is not PageStatus.EXTRACTED or p.text for p in result.pages)


def test_an_empty_file_is_reported_not_crashed() -> None:
    result = PypdfPageTextExtractor().extract(b"")
    assert result.error is not None


# ---------------------------------------------------------------- bounds


def test_page_count_is_bounded() -> None:
    extractor = PypdfPageTextExtractor(max_pages=3)
    result = extractor.extract(pdf.text_pdf([f"PAGE {i}" for i in range(1, 7)]))
    assert result.page_count == 6
    assert result.truncated is True
    assert any(p.status is PageStatus.SKIPPED_AFTER_LIMIT for p in result.pages)
    # Everything past the cap is reported, not silently dropped.
    assert result.pages[-1].page_number == 4
    assert "6 pages" in result.pages[-1].text


def test_the_default_page_cap_is_finite() -> None:
    assert 0 < MAX_PAGES <= 5000


def test_a_tiny_time_budget_is_reported_not_silently_dropped() -> None:
    extractor = PypdfPageTextExtractor(per_page_seconds=0.000001, max_pages=2)
    result = extractor.extract(pdf.text_pdf([f"PAGE {i}" for i in range(1, 3)]))
    assert result.truncated is True
    assert any(p.status is PageStatus.TRUNCATED for p in result.pages)


# ---------------------------------------------------------------- preview contract


def test_preview_is_never_authoritative() -> None:
    result = PypdfPageTextExtractor().extract(pdf.text_pdf(["PAGE ONE DATA"]))
    preview = preview_for_page(result, 1)
    assert preview.is_authoritative is False
    assert "original" in preview.note.lower()
    assert "PAGE ONE DATA" in preview.text


def test_preview_for_a_page_outside_the_result_says_so() -> None:
    result = PypdfPageTextExtractor().extract(pdf.text_pdf(["ONLY PAGE"]))
    preview = preview_for_page(result, 7)
    assert "outside" in preview.text
    assert preview.is_authoritative is False


# ---------------------------------------------------------------- integration


def test_intake_uses_page_accurate_text(session, engine, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The intake path reports per-page text, so a citation points at a real page."""
    from local_ledger.db import init_db
    from local_ledger.tax import Jurisdiction
    from local_ledger.tax.intake import ingest_document, inspect_file_content
    from local_ledger.tax.models import DocumentType, ScanState
    from local_ledger.tax.service import TaxWorkspaceService

    init_db(engine)
    monkeypatch.setenv("LOCAL_LEDGER_TAX_DIR", str(tmp_path / "tax"))
    ws = TaxWorkspaceService(session).get_or_create_workspace(2025, Jurisdiction.FEDERAL)

    document = pdf.text_pdf(
        [
            "PAGE ONE CONSOLIDATED 1099 DIVIDENDS $3,850.00",
            "PAGE TWO INTEREST INCOME $1,425.50",
        ]
    )
    state, pages, texts = inspect_file_content(document, "brokerage.pdf")
    assert state == ScanState.TEXT_EXTRACTABLE
    assert pages == 2
    assert "1,425.50" in texts[2]
    assert "3,850.00" not in texts[2]

    result = ingest_document(
        session=session,
        workspace_id=ws.id,
        file_name="brokerage.pdf",
        file_bytes=document,
        doc_type=DocumentType.FORM_1099_CONSOLIDATED,
    )
    assert result.document.page_count == 2
    # The original is retained for visual verification, byte for byte.
    stored = Path(result.document.storage_path).read_bytes()
    assert stored == document
    assert result.document.sha256_hash
