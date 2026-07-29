"""CSV import tests: parsing, column mapping, ImportPrefs auto-suggest, row errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.ingest.csv_import import (
    ColumnMapping,
    load_import_prefs,
    parse_csv,
    save_import_prefs,
    suggest_mapping,
)
from tests.factories import make_account

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


def _us_single_amount_mapping(headers: list[str]) -> ColumnMapping:
    return ColumnMapping(
        date_col="Date",
        amount_col="Amount",
        payee_col="Description",
        date_format="%Y-%m-%d",
        locale="us",
    )


def test_bank_a_us_parses_valid_rows_and_reports_bad_row() -> None:
    text = (FIXTURES / "bank_a_us.csv").read_text(encoding="utf-8")
    result = parse_csv(text, mapping=_us_single_amount_mapping(["Date", "Description", "Amount"]))

    assert len(result.transactions) == 5  # 6 rows minus the bad-amount row
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.line_number > 1  # not the header
    assert "NOTANUMBER" in err.raw or "Amount" in err.raw
    # spot-check a parsed row
    coffee = next(t for t in result.transactions if t.payee_raw == "Blue Bottle Coffee")
    assert coffee.amount_cents == -450
    assert coffee.date == "2026-01-05"


def test_bank_b_eu_parses_with_eu_locale() -> None:
    text = (FIXTURES / "bank_b_eu.csv").read_text(encoding="utf-8")
    mapping = ColumnMapping(
        date_col="Datum",
        amount_col="Betrag",
        payee_col="Beschreibung",
        date_format="%d.%m.%Y",
        locale="eu",
    )
    result = parse_csv(text, mapping=mapping)
    assert len(result.transactions) == 3
    assert result.transactions[0].amount_cents == -1234
    assert result.transactions[0].date == "2026-01-05"


def test_bank_c_debit_credit_columns() -> None:
    text = (FIXTURES / "bank_c_debit_credit.csv").read_text(encoding="utf-8")
    mapping = ColumnMapping(
        date_col="Date",
        debit_col="Debit",
        credit_col="Credit",
        payee_col="Payee",
        date_format="%Y-%m-%d",
        locale="us",
    )
    result = parse_csv(text, mapping=mapping)
    by_payee = {t.payee_raw: t.amount_cents for t in result.transactions}
    assert by_payee["Landlord"] == -150000
    assert by_payee["Refund"] == 4500
    assert by_payee["Cafe"] == -1250


def test_suggest_mapping_guesses_common_headers() -> None:
    mapping = suggest_mapping(["Date", "Description", "Amount"])
    assert mapping.date_col == "Date"
    assert mapping.amount_col == "Amount"
    assert mapping.payee_col == "Description"


def test_suggest_mapping_handles_eu_and_debit_credit() -> None:
    eu = suggest_mapping(["Datum", "Beschreibung", "Betrag"])
    assert eu.date_col == "Datum"
    assert eu.amount_col == "Betrag"
    assert eu.locale == "eu"

    dc = suggest_mapping(["Date", "Payee", "Debit", "Credit"])
    assert dc.debit_col == "Debit"
    assert dc.credit_col == "Credit"


def test_import_prefs_round_trip_and_auto_suggest(session: Session) -> None:
    acc = make_account(session)
    session.commit()
    mapping = _us_single_amount_mapping(["Date", "Description", "Amount"])

    # no prefs yet -> None
    assert load_import_prefs(session, acc.id) is None

    save_import_prefs(session, acc.id, mapping)
    loaded = load_import_prefs(session, acc.id)
    assert loaded is not None
    assert loaded.amount_col == "Amount"
    assert loaded.date_col == "Date"


def test_bad_row_does_not_block_good_rows() -> None:
    """AC: a single malformed row produces 1 error; valid rows still parse."""
    text = "Date,Payee,Amount\n2026-01-01,A,-1.00\n2026-01-02,B,garbage\n2026-01-03,C,-2.00\n"
    mapping = _us_single_amount_mapping(["Date", "Payee", "Amount"])
    mapping.payee_col = "Payee"
    result = parse_csv(text, mapping=mapping)
    assert len(result.transactions) == 2
    assert len(result.errors) == 1
