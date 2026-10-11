"""Per-page PDF text extraction, behind a small swappable interface.

The first implementation is pypdf (BSD-3-Clause, pure Python). A table-aware
extractor such as pdfplumber can replace it later without touching callers -
that is the point of the :class:`PageTextExtractor` protocol: intake depends on
"give me each page's text and say why it is empty", not on a PDF library.

Four rules that shaped this, each from a way a real document can go wrong:

**The extracted text is not the authoritative view.** Extraction can scramble
columns and reorder glyphs, so it is a convenience for locating a value. The
stored original PDF stays on disk and is what a reviewer verifies against
(contracts §11, TAX-05 acceptance "verified against a visible source"). Nothing
here replaces or discards the original.

**An empty page means "no extractable text on this page", not "no text
anywhere".** A broker statement can have a text cover page and image-only
statement pages. Each page carries its own status, and a page that yielded
nothing gets a note saying exactly that - never the whole document's text, which
would let a reviewer verify a field against the wrong page without noticing.

**Extraction is bounded.** A small compressed file can expand enormously when
decompressed, and a malformed document can send a parser into a long loop. Page
count, per-page and total time, and extracted-size budgets are enforced here.
Nothing about a hostile document should be able to hang intake.

**pypdf does not OCR.** Encrypted, malformed and image-only documents are
reported with a reason and routed to manual field entry. That is a scoped
extension, not a silent empty success.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

#: Hard bounds. A tax return is tens of pages; these are generous but finite.
MAX_PAGES = 500
PER_PAGE_SECONDS = 5.0
TOTAL_SECONDS = 60.0
#: Guard against a decompression bomb: a page yielding far more text than any
#: real form is truncated and flagged rather than trusted.
MAX_CHARS_PER_PAGE = 500_000


class PageStatus(StrEnum):
    """Per-page outcome. Never collapsed into a document-level guess."""

    EXTRACTED = "EXTRACTED"
    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    TRUNCATED = "TRUNCATED"
    TIMED_OUT = "TIMED_OUT"
    SKIPPED_AFTER_LIMIT = "SKIPPED_AFTER_LIMIT"
    ERROR = "ERROR"


@dataclass(frozen=True)
class PageText:
    """One page's extraction result.

    ``page_number`` is the page's 1-based position in the original document, so a
    citation recorded from it points at the real page a reviewer will see.
    """

    page_number: int
    text: str
    status: PageStatus

    @property
    def has_text(self) -> bool:
        return self.status in (PageStatus.EXTRACTED, PageStatus.TRUNCATED)


@dataclass(frozen=True)
class ExtractionResult:
    """Whole-document outcome.

    ``is_scanned`` is only true when every inspected page has no text layer.
    Parser errors, timeouts and uninspected pages do not establish that a
    document is scanned.
    """

    pages: tuple[PageText, ...]
    page_count: int
    document_note: str = ""
    truncated: bool = False
    error: str | None = None

    @property
    def pages_with_text(self) -> tuple[PageText, ...]:
        return tuple(p for p in self.pages if p.has_text)

    @property
    def is_scanned(self) -> bool:
        """No text layer was found on every page, without extraction failures."""
        return bool(self.pages) and all(
            page.status == PageStatus.NO_TEXT_LAYER for page in self.pages
        )


class PageTextExtractor(Protocol):
    """The interface intake depends on. Swap the implementation, not the callers."""

    name: str

    def extract(self, pdf_bytes: bytes) -> ExtractionResult:  # pragma: no cover - protocol
        ...


class PypdfPageTextExtractor:
    """pypdf-backed implementation.

    Uses ``PdfReader`` for page iteration and decoding, which is what makes page
    attribution correct: the Nth entry always belongs to the Nth page, whatever
    order the file happens to store its objects in.
    """

    name = "pypdf"

    def __init__(
        self,
        *,
        max_pages: int = MAX_PAGES,
        per_page_seconds: float = PER_PAGE_SECONDS,
        total_seconds: float = TOTAL_SECONDS,
        layout: bool = False,
    ) -> None:
        self.max_pages = max_pages
        self.per_page_seconds = per_page_seconds
        self.total_seconds = total_seconds
        self.layout = layout

    def extract(self, pdf_bytes: bytes) -> ExtractionResult:
        from pypdf import PdfReader

        try:
            reader = PdfReader(_as_stream(pdf_bytes), strict=False)
        except Exception as exc:  # malformed xref, bad object, truncated file
            return ExtractionResult(pages=(), page_count=0, error=f"could not parse PDF: {exc}")

        if reader.is_encrypted:
            # pypdf cannot read a document whose password is unknown. Not an
            # error to retry: it is a document that needs manual entry.
            try:
                if reader.decrypt("") == 0:
                    return ExtractionResult(
                        pages=(),
                        page_count=0,
                        error="PDF is encrypted; open it and enter fields manually",
                    )
            except Exception as exc:
                return ExtractionResult(
                    pages=(),
                    page_count=0,
                    error=f"PDF is encrypted and could not be opened: {exc}",
                )

        try:
            total_pages = len(reader.pages)
        except Exception as exc:
            return ExtractionResult(
                pages=(), page_count=0, error=f"could not read the page tree: {exc}"
            )

        if total_pages == 0:
            return ExtractionResult(pages=(), page_count=0, error="PDF contains no pages")

        started = time.monotonic()
        pages: list[PageText] = []
        truncated = False
        limit_hit = total_pages > self.max_pages

        for index in range(min(total_pages, self.max_pages)):
            page_number = index + 1
            if time.monotonic() - started > self.total_seconds:
                truncated = True
                pages.append(
                    PageText(
                        page_number=page_number,
                        text=(
                            f"(Page {page_number} was not read: extraction time budget of "
                            f"{self.total_seconds:.0f}s was reached. Enter fields manually.)"
                        ),
                        status=PageStatus.TIMED_OUT,
                    )
                )
                continue

            page_started = time.monotonic()
            try:
                page = reader.pages[index]
                raw = page.extract_text(
                    extraction_mode="layout" if self.layout else "plain"
                )
            except Exception as exc:
                pages.append(
                    PageText(
                        page_number=page_number,
                        text=(
                            f"(Page {page_number} could not be read: {exc}. "
                            "Enter fields manually.)"
                        ),
                        status=PageStatus.ERROR,
                    )
                )
                continue

            elapsed = time.monotonic() - page_started
            text = (raw or "").strip()
            if not text:
                pages.append(
                    PageText(
                        page_number=page_number,
                        text=(
                            f"(Page {page_number} has no extractable text - it may be an "
                            "image or scan. pypdf does not perform OCR; enter fields manually.)"
                        ),
                        status=PageStatus.NO_TEXT_LAYER,
                    )
                )
                continue

            if len(text) > MAX_CHARS_PER_PAGE or elapsed > self.per_page_seconds:
                truncated = True
                pages.append(
                    PageText(
                        page_number=page_number,
                        text=text[:MAX_CHARS_PER_PAGE],
                        status=PageStatus.TRUNCATED,
                    )
                )
                continue

            pages.append(PageText(page_number=page_number, text=text, status=PageStatus.EXTRACTED))

        if limit_hit:
            truncated = True
            pages.append(
                PageText(
                    page_number=min(total_pages, self.max_pages) + 1,
                    text=(
                        f"(Document has {total_pages} pages; only the first {self.max_pages} "
                        "were read. Enter fields from the remaining pages manually.)"
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

        return ExtractionResult(
            pages=tuple(pages),
            page_count=total_pages,
            document_note=note,
            truncated=truncated,
        )


def _as_stream(pdf_bytes: bytes):  # type: ignore[no-untyped-def]
    import io

    return io.BytesIO(pdf_bytes)


def extract_pages(
    pdf_bytes: bytes,
    *,
    extractor: PageTextExtractor | None = None,
) -> ExtractionResult:
    """Extract in a worker by default, honoring the explicit environment override."""
    if extractor is None:
        from local_ledger.tax.pdf_worker import default_extractor

        extractor = default_extractor()
    return extractor.extract(pdf_bytes)


@dataclass
class TextPreview:
    """What the review UI shows for one page, and what it is allowed to claim."""

    page_number: int
    text: str
    status: PageStatus
    is_authoritative: bool = False
    note: str = ""


def preview_for_page(result: ExtractionResult, page_number: int) -> TextPreview:
    """Build the preview block for one page of an extraction result.

    ``is_authoritative`` is always False: extracted text is a locating aid, and
    the reviewer verifies against the stored original. Marking it here means no
    caller has to remember.
    """
    match = next((p for p in result.pages if p.page_number == page_number), None)
    if match is None:
        return TextPreview(
            page_number=page_number,
            text="(This page is outside the pages that were read.)",
            status=PageStatus.SKIPPED_AFTER_LIMIT,
            note=result.document_note,
        )
    return TextPreview(
        page_number=match.page_number,
        text=match.text,
        status=match.status,
        is_authoritative=False,
        note=(
            "Extracted text is a locating aid only - columns can be reordered. "
            "Verify the value against the original document."
        ),
    )
