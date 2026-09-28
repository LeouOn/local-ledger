"""Private tax document storage and integrity management.

Handles private filesystem storage for source tax documents outside the git
repository. Documents are stored immutably with read-only permissions and
content-addressable/hashed tracking. Sensitive paths and PII (SSNs, account numbers)
are masked before being included in logs or error messages.

Storage Location and Retention Policy:
-------------------------------------
1. Root directory is configurable via `LOCAL_LEDGER_TAX_DIR`. If unset, it defaults
   to `~/.local/share/local-ledger/tax_documents/`.
2. Documents are organized by workspace year: `<tax_storage_root>/<tax_year>/<doc_id>_<safe_name>`.
3. Originals are written immutably (read-only mode 0o400) and are never modified in place.
4. When a corrected document is ingested, it is stored as a distinct file; the original
   remains in place for audit trail and provenance verification.
5. Path validation prevents directory traversal attacks outside the storage root.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path

# SSN pattern: 123-45-6789 or continuous 9 digits in tax context
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_RAW_SSN_PATTERN = re.compile(r"\b(?:\d{9})\b")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


class TaxStorageError(Exception):
    """Base error for tax document storage issues."""


class DocumentIntegrityError(TaxStorageError):
    """Raised when stored file SHA-256 does not match expected hash."""


class DocumentNotFoundError(TaxStorageError):
    """Raised when a referenced document does not exist in storage."""


def mask_pii(text: str) -> str:
    """Mask Social Security Numbers and sensitive tax identification numbers."""
    if not text:
        return ""
    masked = _SSN_PATTERN.sub("***-**-****", text)
    # Mask 9-digit sequences that look like unhyphenated SSNs
    masked = _RAW_SSN_PATTERN.sub("*********", masked)
    return masked


def sanitize_path(path: Path | str) -> str:
    """Mask absolute user directory and private paths for error logs."""
    if not path:
        return ""
    p_str = str(path)
    tax_dir = os.environ.get("LOCAL_LEDGER_TAX_DIR")
    if tax_dir:
        p_str = p_str.replace(tax_dir, "[TAX_STORAGE]")

    home = str(Path.home())
    if home:
        p_str = p_str.replace(home, "~")

    return mask_pii(p_str)


def get_tax_storage_dir() -> Path:
    """Return configured tax storage root, creating it if necessary."""
    env_dir = os.environ.get("LOCAL_LEDGER_TAX_DIR")
    if env_dir:
        root = Path(env_dir).resolve()
    else:
        root = Path.home() / ".local" / "share" / "local-ledger" / "tax_documents"
    root.mkdir(parents=True, exist_ok=True)
    return root


def compute_sha256(data: bytes | Path) -> str:
    """Compute SHA-256 hash over bytes or a file path."""
    hasher = hashlib.sha256()
    if isinstance(data, (bytes, bytearray)):
        hasher.update(data)
    else:
        with open(data, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def detect_pdf_pages_and_text(data: bytes) -> tuple[str, int]:
    """Inspect PDF content to detect page count and whether text is extractable."""
    if not data.startswith(b"%PDF"):
        # Not a PDF (could be JSON fixture, CSV, image)
        return "text_extractable", 1

    # Simple inspection of page objects
    page_matches = re.findall(rb"/Type\s*/Page\b", data)
    page_count = max(len(page_matches), 1)

    # Check for text showing operators (BT ... ET, Tj, TJ)
    has_text = bool(re.search(rb"\bBT\b", data) or re.search(rb"/Font\b", data))
    scan_state = "text_extractable" if has_text else "scanned_no_text"
    return scan_state, page_count


def store_document_file(
    source: Path | bytes,
    file_name: str,
    tax_year: int,
    doc_id: str,
) -> tuple[str, str, int, str]:
    """Store a private document immutably in private tax storage.

    Returns:
        (storage_path, sha256_hash, page_count, scan_state)
    """
    if isinstance(source, (bytes, bytearray)):
        content = bytes(source)
    else:
        source_path = Path(source)
        if not source_path.exists():
            raise DocumentNotFoundError(f"Source file not found: {sanitize_path(source_path)}")
        content = source_path.read_bytes()

    sha256 = compute_sha256(content)
    scan_state, page_count = detect_pdf_pages_and_text(content)

    clean_name = _SAFE_FILENAME_RE.sub("_", Path(file_name).name)
    target_dir = get_tax_storage_dir() / str(tax_year)
    target_dir.mkdir(parents=True, exist_ok=True)

    dest_file = target_dir / f"{doc_id}_{clean_name}"

    # Write file
    dest_file.write_bytes(content)
    # Set read-only permissions for immutability (0o400: user read-only)
    with contextlib.suppress(OSError):
        dest_file.chmod(0o400)

    return str(dest_file.resolve()), sha256, page_count, scan_state


def verify_stored_document(storage_path_str: str, expected_sha256: str) -> Path:
    """Verify stored file exists, is inside storage dir, and matches SHA-256.

    Raises:
        DocumentNotFoundError if file is missing.
        DocumentIntegrityError if hash does not match or traversal attempted.
    """
    path = Path(storage_path_str).resolve()
    storage_root = get_tax_storage_dir().resolve()

    # Prevent directory traversal
    try:
        path.relative_to(storage_root)
    except ValueError as err:
        raise DocumentIntegrityError(
            f"Access denied: path is outside tax storage root: {sanitize_path(path)}"
        ) from err

    if not path.is_file():
        raise DocumentNotFoundError(f"Document file missing on disk: {sanitize_path(path)}")

    current_hash = compute_sha256(path)
    if current_hash != expected_sha256:
        raise DocumentIntegrityError(
            f"Integrity check failed for document {sanitize_path(path)}: hash mismatch"
        )

    return path
