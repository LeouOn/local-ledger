"""Strict amount/date parsing.

Ambiguous formats are rejected with ``AmbiguousAmountError`` rather than
silently coerced - a 100x monetary error from a misread locale is the worst
possible failure mode for a finance app.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

Locale = Literal["us", "eu"]

_CURRENCY_SYMBOLS = re.compile(r"[\$£€¥]")
_US_AMOUNT = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?$|^-?\d+(?:\.\d{1,2})?$")
_EU_AMOUNT = re.compile(r"^-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?$|^-?\d+(?:,\d{1,2})?$")
_PLAIN_INT = re.compile(r"^-?\d+$")


class AmbiguousAmountError(ValueError):
    pass


def _strip_symbol(raw: str) -> str:
    return _CURRENCY_SYMBOLS.sub("", raw).strip()


def parse_amount_to_cents(raw: str, *, locale: Locale, cents_mode: bool = False) -> int:
    cleaned = _strip_symbol(str(raw)).strip()
    if cleaned == "":
        raise AmbiguousAmountError(f"empty amount string for locale={locale}")

    if cents_mode:
        if not _PLAIN_INT.match(cleaned):
            raise AmbiguousAmountError(f"cents_mode requires a plain integer, got {raw!r}")
        return int(cleaned)

    # A pure integer with no decimal/thousands separator is ambiguous: it could
    # be whole-currency units or already-cents. Force the caller to disambiguate
    # via cents_mode rather than guess (a wrong guess is a 100x error).
    if _PLAIN_INT.match(cleaned):
        raise AmbiguousAmountError(
            f"plain integer {raw!r} is ambiguous (dollars or cents?); pass cents_mode=True if already cents"
        )

    if locale == "us":
        if not _US_AMOUNT.match(cleaned):
            raise AmbiguousAmountError(f"unparseable US amount {raw!r}")
        # US: comma = thousands sep, dot = decimal
        normalized = cleaned.replace(",", "")
        return _to_cents(normalized, decimal_sep=".")
    elif locale == "eu":
        if not _EU_AMOUNT.match(cleaned):
            raise AmbiguousAmountError(f"unparseable EU amount {raw!r}")
        # EU: dot = thousands sep, comma = decimal
        normalized = cleaned.replace(".", "").replace(",", ".")
        return _to_cents(normalized, decimal_sep=".")
    else:  # pragma: no cover
        raise AmbiguousAmountError(f"unknown locale {locale!r}")


def _to_cents(normalized: str, *, decimal_sep: str) -> int:
    if decimal_sep in normalized:
        whole, frac = normalized.split(decimal_sep, 1)
        frac = (frac + "00")[:2]
        sign = -1 if whole.startswith("-") else 1
        whole_abs = whole.lstrip("-")
        return sign * (int(whole_abs or "0") * 100 + int(frac))
    sign = -1 if normalized.startswith("-") else 1
    return sign * int(normalized.lstrip("-")) * 100


def detect_format(samples: list[str]) -> Locale | None:
    """Suggest a locale from a column of sample values, or None if ambiguous."""
    seen: set[Locale] = set()
    for raw in samples:
        cleaned = _strip_symbol(str(raw)).strip()
        if cleaned == "":
            continue
        if _PLAIN_INT.match(cleaned):
            continue  # plain integer - locale-ambiguous, skip
        if _US_AMOUNT.match(cleaned) and not _EU_AMOUNT.match(cleaned):
            seen.add("us")
        elif _EU_AMOUNT.match(cleaned) and not _US_AMOUNT.match(cleaned):
            seen.add("eu")
        elif _US_AMOUNT.match(cleaned) and _EU_AMOUNT.match(cleaned):
            # both match (e.g. "1.234" - dot but no comma) -> ambiguous
            return None
    if len(seen) != 1:
        return None
    return next(iter(seen))


def parse_date(raw: str, fmt: str) -> str:
    """Parse ``raw`` using the explicit ``fmt`` and return canonical YYYY-MM-DD."""
    parsed = datetime.strptime(raw.strip(), fmt)
    return parsed.strftime("%Y-%m-%d")
