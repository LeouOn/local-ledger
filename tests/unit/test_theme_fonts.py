"""Validate that the vendored Alegreya fonts are present and well-formed."""

from __future__ import annotations

from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parents[2] / "src" / "local_ledger" / "ui" / "fonts"


def test_alegreya_regular_woff2_exists_and_has_wOF2_magic() -> None:
    p = FONTS_DIR / "alegreya-regular.woff2"
    assert p.exists(), f"missing {p}"
    data = p.read_bytes()
    assert data[:4] == b"wOF2", f"not a woff2 file: starts with {data[:4]!r}"
    assert len(data) > 30_000, f"regular file too small ({len(data)} B), probably truncated"


def test_alegreya_bold_woff2_exists_and_has_wOF2_magic() -> None:
    p = FONTS_DIR / "alegreya-bold.woff2"
    assert p.exists(), f"missing {p}"
    data = p.read_bytes()
    assert data[:4] == b"wOF2", f"not a woff2 file: starts with {data[:4]!r}"
    assert len(data) > 30_000, f"bold file too small ({len(data)} B), probably truncated"


def test_alegreya_license_present() -> None:
    p = FONTS_DIR / "LICENSE.txt"
    assert p.exists(), f"missing {p}"
    text = p.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text
    assert "Alegreya" in text
