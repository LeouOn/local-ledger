"""CSV import: dialect sniffing, column mapping, per-row error collection.

Bad rows never silently disappear and never block good rows. Per-account
ImportPrefs persist the last mapping so the next import auto-suggests it.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass

from sqlmodel import Session

from local_ledger.ingest.amounts import (
    AmbiguousAmountError,
    Locale,
    detect_format,
    parse_amount_to_cents,
    parse_date,
)
from local_ledger.models import ImportPrefs


@dataclass
class ColumnMapping:
    date_col: str
    amount_col: str | None = None
    debit_col: str | None = None
    credit_col: str | None = None
    payee_col: str | None = None
    notes_col: str | None = None
    imported_id_col: str | None = None
    date_format: str = "%Y-%m-%d"
    locale: Locale = "us"
    cents_mode: bool = False


@dataclass
class ParsedTransaction:
    date: str
    amount_cents: int
    payee_raw: str | None = None
    notes: str | None = None
    imported_id: str | None = None
    # 1-based record number in the source file (the header is record 1), so a
    # committed transaction can be traced back to a line the user can open.
    source_row: int | None = None


@dataclass
class ParseError:
    line_number: int
    raw: str
    reason: str


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction]
    errors: list[ParseError]


_DATE_HEADERS = {"date", "datum", "transaction date", "posted date", "post date"}
_AMOUNT_HEADERS = {"amount", "betrag", "transaction amount"}
_DEBIT_HEADERS = {"debit", "withdrawal", "withdrawals", "ausgang"}
_CREDIT_HEADERS = {"credit", "deposit", "deposits", "eingang"}
_PAYEE_HEADERS = {"description", "payee", "merchant", "name", "beskrivning", "beschreibung", "details"}
_NOTES_HEADERS = {"notes", "memo", "category"}
_ID_HEADERS = {"id", "fitid", "transaction id", "reference"}
_EU_DATE_FORMATS = ("%d.%m.%Y", "%d/%m/%Y")


def suggest_mapping(headers: list[str]) -> ColumnMapping:
    norm = {h.lower().strip(): h for h in headers}

    def find(candidates: set[str]) -> str | None:
        for c in candidates:
            if c in norm:
                return norm[c]
        return None

    date_col = find(_DATE_HEADERS) or headers[0]
    amount_col = find(_AMOUNT_HEADERS)
    debit_col = find(_DEBIT_HEADERS)
    credit_col = find(_CREDIT_HEADERS)
    payee_col = find(_PAYEE_HEADERS)
    notes_col = find(_NOTES_HEADERS)
    imported_id_col = find(_ID_HEADERS)

    # Heuristic date format/locale from header language + a sample guess.
    date_format = "%Y-%m-%d"
    locale: Locale = "us"
    if "datum" in norm:
        date_format = "%d.%m.%Y"
        locale = "eu"
    if "betrag" in norm or "beschreibung" in norm:
        locale = "eu"

    return ColumnMapping(
        date_col=date_col,
        amount_col=amount_col,
        debit_col=debit_col,
        credit_col=credit_col,
        payee_col=payee_col,
        notes_col=notes_col,
        imported_id_col=imported_id_col,
        date_format=date_format,
        locale=locale,
    )


def save_import_prefs(session: Session, account_id: int, mapping: ColumnMapping) -> ImportPrefs:
    prefs = ImportPrefs(account_id=account_id, mapping_json=json.dumps(asdict(mapping)))
    session.merge(prefs)
    session.commit()
    return prefs


def load_import_prefs(session: Session, account_id: int) -> ColumnMapping | None:
    row = session.get(ImportPrefs, account_id)
    if row is None:
        return None
    return ColumnMapping(**json.loads(row.mapping_json))


def _sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel  # type: ignore[return-value]


def _resolve_amount(row: dict[str, str], mapping: ColumnMapping) -> int:
    if mapping.amount_col is not None:
        return parse_amount_to_cents(
            row.get(mapping.amount_col, ""), locale=mapping.locale, cents_mode=mapping.cents_mode
        )
    # debit/credit pair
    debit_raw = row.get(mapping.debit_col or "", "") or ""
    credit_raw = row.get(mapping.credit_col or "", "") or ""
    if debit_raw.strip():
        amt = parse_amount_to_cents(debit_raw, locale=mapping.locale, cents_mode=mapping.cents_mode)
        return -abs(amt)  # debit column = money out (negative)
    if credit_raw.strip():
        amt = parse_amount_to_cents(credit_raw, locale=mapping.locale, cents_mode=mapping.cents_mode)
        return abs(amt)  # credit column = money in (positive)
    raise AmbiguousAmountError("both debit and credit columns empty")


def parse_csv(text: str, *, mapping: ColumnMapping) -> ParseResult:
    sample = text[:4096]
    dialect = _sniff_dialect(sample)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    transactions: list[ParsedTransaction] = []
    errors: list[ParseError] = []

    for line_no, row in enumerate(reader, start=2):  # header is record 1
        try:
            amount = _resolve_amount(row, mapping)
            date = parse_date(row.get(mapping.date_col, ""), mapping.date_format)
            transactions.append(
                ParsedTransaction(
                    date=date,
                    amount_cents=amount,
                    payee_raw=(row.get(mapping.payee_col) if mapping.payee_col else None),
                    notes=(row.get(mapping.notes_col) if mapping.notes_col else None),
                    imported_id=(row.get(mapping.imported_id_col) if mapping.imported_id_col else None),
                    source_row=line_no,
                )
            )
        except (AmbiguousAmountError, ValueError) as exc:
            errors.append(
                ParseError(
                    line_number=line_no,
                    raw=json.dumps({k: v for k, v in row.items() if v}, sort_keys=True),
                    reason=str(exc),
                )
            )

    return ParseResult(transactions=transactions, errors=errors)


def auto_detect_locale(amount_samples: list[str]) -> Locale | None:
    return detect_format(amount_samples)
