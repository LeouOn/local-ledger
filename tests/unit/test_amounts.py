"""Amount/date parsing primitives - strict, ambiguous-format-rejecting.

The full Metis AC11 matrix. Silent 100x monetary errors are the failure mode
these tests guard against.
"""

from __future__ import annotations

import pytest

from local_ledger.ingest.amounts import (
    AmbiguousAmountError,
    detect_format,
    parse_amount_to_cents,
    parse_date,
)

# --- amount parsing matrix (AC11) ---


@pytest.mark.parametrize(
    "raw,locale,expected",
    [
        ("123.45", "us", 12345),
        ("1,234.56", "us", 123456),
        ("-123.45", "us", -12345),
        ("1.234,56", "eu", 123456),
        ("-1.234,56", "eu", -123456),
        ("$123.45", "us", 12345),
    ],
)
def test_amount_parse_happy(raw, locale, expected) -> None:
    assert parse_amount_to_cents(raw, locale=locale) == expected


def test_amount_cents_mode_explicit() -> None:
    assert parse_amount_to_cents("12345", locale="us", cents_mode=True) == 12345


def test_amount_rejects_ambiguous_plain_digits_without_cents_mode() -> None:
    with pytest.raises(AmbiguousAmountError):
        parse_amount_to_cents("12345", locale="us")


def test_amount_rejects_eu_format_under_us_locale() -> None:
    """A EU-formatted string under a US locale is ambiguous, never silently wrong."""
    with pytest.raises(AmbiguousAmountError):
        parse_amount_to_cents("1.234,56", locale="us")


def test_amount_rejects_empty_string() -> None:
    with pytest.raises(AmbiguousAmountError):
        parse_amount_to_cents("", locale="us")


def test_amount_rejects_non_numeric() -> None:
    with pytest.raises(AmbiguousAmountError):
        parse_amount_to_cents("abc", locale="us")


# --- format detection ---


@pytest.mark.parametrize(
    "samples,expected",
    [
        (["123.45", "1,234.56", "-9.99"], "us"),
        (["1.234,56", "-9,99", "0,50"], "eu"),
        (["12345", "67890"], None),  # plain integers - ambiguous
        (["123.45", "1.234,56"], None),  # mixed - ambiguous
    ],
)
def test_detect_format(samples, expected) -> None:
    assert detect_format(samples) == expected


# --- date parsing ---


@pytest.mark.parametrize(
    "raw,fmt,expected",
    [
        ("2026-01-15", "%Y-%m-%d", "2026-01-15"),
        ("15/01/2026", "%d/%m/%Y", "2026-01-15"),
        ("01/15/2026", "%m/%d/%Y", "2026-01-15"),
        ("20260115", "%Y%m%d", "2026-01-15"),
    ],
)
def test_parse_date_formats(raw, fmt, expected) -> None:
    assert parse_date(raw, fmt) == expected


def test_parse_date_rejects_unknown_format() -> None:
    with pytest.raises(ValueError):
        parse_date("15-Jan-2026", "%Y-%m-%d")


def test_parse_date_rejects_impossible_date() -> None:
    with pytest.raises(ValueError):
        parse_date("2026-13-45", "%Y-%m-%d")
