"""Builders for genuinely valid synthetic PDFs, shared by the tax intake tests.

The earlier fixtures in this area were hand-written byte strings that *looked*
like PDFs: no cross-reference table, and a font object referenced but never
defined. A regex parser never noticed, because it never built a page tree. A
real reader rejects them, which is the correct outcome - so tests here assemble
structurally valid files instead, with a real xref table, a real font resource,
and optionally FlateDecode compression.

Pure test support: no application code depends on this module.
"""

from __future__ import annotations

import zlib

FONT_OBJECT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def assemble_pdf(objects: dict[int, bytes], root: int = 1) -> bytes:
    """Assemble a minimal but structurally valid PDF from numbered objects."""
    for num in range(1, max(objects) + 1):
        objects.setdefault(num, b"null")
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {size} /Root {root} 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _stream_object(payload: bytes, *, compress: bool) -> bytes:
    if compress:
        data = zlib.compress(payload)
        return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
    return b"<< /Length %d >>\nstream\n" % len(payload) + payload + b"\nendstream"


def text_pdf(
    page_texts: list[str],
    *,
    compress: bool = True,
    extra_objects: dict[int, bytes] | None = None,
) -> bytes:
    """A multi-page PDF with one text-bearing content stream per page."""
    count = len(page_texts)
    kids = [3 + i for i in range(count)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{k} 0 R".encode() for k in kids)
            + b"] /Count %d >>" % count
        ),
        900: FONT_OBJECT,
    }
    if extra_objects:
        objects.update(extra_objects)
    for index, (page_number, text) in enumerate(zip(kids, page_texts, strict=True)):
        payload = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
        content_number = 700 + index
        objects[content_number] = _stream_object(payload, compress=compress)
        objects[page_number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 900 0 R >> >> /Contents %d 0 R >>" % content_number
        )
    return assemble_pdf(objects)


def image_only_pdf(page_count: int = 2, *, compress: bool = True) -> bytes:
    """Pages that draw a filled rectangle and carry no text operators at all.

    This is what a scan looks like to a text extractor: a real page with content,
    and nothing readable.
    """
    kids = [3 + i for i in range(page_count)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{k} 0 R".encode() for k in kids)
            + b"] /Count %d >>" % page_count
        ),
    }
    for index, page_number in enumerate(kids):
        content_number = 600 + index
        objects[content_number] = _stream_object(
            b"0.5 0.5 0.5 rg 100 100 300 400 re f", compress=compress
        )
        objects[page_number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents %d 0 R >>" % content_number
        )
    return assemble_pdf(objects)


def mixed_pdf(text_page: str, blank_pages: int = 1) -> bytes:
    """A text page followed by image-only pages - a very common real shape."""
    kids = [3 + i for i in range(blank_pages + 1)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{k} 0 R".encode() for k in kids)
            + b"] /Count %d >>" % (blank_pages + 1)
        ),
        900: FONT_OBJECT,
    }
    objects[700] = _stream_object(
        f"BT /F1 12 Tf 20 100 Td ({text_page}) Tj ET".encode(), compress=True
    )
    objects[3] = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 900 0 R >> >> /Contents 700 0 R >>"
    )
    for index in range(blank_pages):
        page_number = kids[index + 1]
        content_number = 710 + index
        objects[content_number] = _stream_object(
            b"0.9 0.9 0.9 rg 10 10 500 700 re f", compress=True
        )
        objects[page_number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents %d 0 R >>" % content_number
        )
    return assemble_pdf(objects)


def encrypted_pdf(user_password: str = "owner-secret") -> bytes:
    """A password-protected PDF, built with pypdf so the encryption is real."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password=user_password, owner_password=user_password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
