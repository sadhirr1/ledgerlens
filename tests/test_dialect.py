"""Encoding, delimiter, header-row and column-role detection."""

from __future__ import annotations

import pytest

from ledgerlens.ingest.dialect import (
    DialectError,
    assign_columns,
    detect_delimiter,
    detect_dialect,
    detect_encoding,
    read_rows,
)


def test_us_checking_layout(fixtures):
    d = detect_dialect(fixtures / "main_checking.csv")
    assert d.delimiter == ","
    assert d.header_row == 0
    assert d.columns["date"] == "Posting Date"
    assert d.columns["amount"] == "Amount"
    assert d.columns["description"] == "Description"


def test_balance_column_is_never_mistaken_for_the_amount(fixtures):
    d = detect_dialect(fixtures / "main_checking.csv")
    assert "Balance" not in d.columns.values(), (
        "a running-balance column is numeric and adjacent to the amount; "
        "picking it would invert every total in the database"
    )


def test_split_debit_credit_columns(fixtures):
    d = detect_dialect(fixtures / "uk_current.csv")
    assert d.has_debit_credit
    assert d.columns["debit"] == "Paid Out"
    assert d.columns["credit"] == "Paid In"
    assert "amount" not in d.columns


def test_european_semicolons_and_cp1252(fixtures):
    path = fixtures / "euro_semicolon.csv"
    assert detect_encoding(path) in ("cp1252", "latin-1")
    d = detect_dialect(path)
    assert d.delimiter == ";"
    assert d.columns["date"] == "Buchungstag"
    assert d.columns["amount"] == "Betrag"


def test_preamble_above_the_header_is_skipped(fixtures):
    d = detect_dialect(fixtures / "amex_preamble.csv")
    assert d.header_row == 4, "the real header sits below four rows of account blurb"
    rows = read_rows(fixtures / "amex_preamble.csv", d)
    assert all("Date" in r for r in rows)
    assert rows[0]["Description"]


def test_type_column_detected_alongside_unsigned_amount(fixtures):
    d = detect_dialect(fixtures / "credit_union.csv")
    assert d.columns["type"] == "DR/CR"
    assert d.columns["amount"] == "Amount"
    assert d.columns["description"] == "Narrative"


@pytest.mark.parametrize(
    ("header", "role", "expected"),
    [
        (["Trans Date", "Payee", "Amount"], "date", "Trans Date"),
        (["Date", "Posted Date", "Merchant", "Amount"], "date", "Date"),
        (["Date", "Narrative", "Value"], "amount", "Value"),
        (["Date", "Memo", "Withdrawal", "Deposit"], "debit", "Withdrawal"),
        (["Date", "Memo", "Withdrawal", "Deposit"], "credit", "Deposit"),
        (["Date", "Original Description", "Amount"], "description", "Original Description"),
    ],
)
def test_synonym_assignment(header, role, expected):
    assert assign_columns(header)[role] == expected


def test_lone_debit_column_is_treated_as_the_amount():
    # A "Withdrawal" column with no "Deposit" counterpart is just the amount.
    assigned = assign_columns(["Date", "Description", "Withdrawal"])
    assert assigned.get("amount") == "Withdrawal"
    assert "debit" not in assigned


def test_no_column_is_assigned_to_two_roles():
    assigned = assign_columns(["Date", "Description", "Amount", "Type", "Balance"])
    assert len(set(assigned.values())) == len(assigned)


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("a,b,c\n1,2,3\n4,5,6", ","),
        ("a;b;c\n1;2;3\n4;5;6", ";"),
        ("a\tb\tc\n1\t2\t3\n4\t5\t6", "\t"),
    ],
)
def test_delimiter_detection(sample, expected):
    assert detect_delimiter(sample) == expected


def test_unrecognisable_file_raises_with_a_useful_message(tmp_path):
    junk = tmp_path / "junk.csv"
    junk.write_text("alpha,beta,gamma\n1,2,3\n", encoding="utf-8")
    with pytest.raises(DialectError) as exc:
        detect_dialect(junk)
    assert "ledgerlens.yaml" in str(exc.value), "the error should say how to fix it"


def test_empty_file_raises(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(DialectError):
        detect_dialect(empty)


def test_short_rows_are_padded_not_dropped(tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text(
        "Date,Description,Amount,Balance\n2026-01-02,COFFEE,-4.50\n", encoding="utf-8"
    )
    d = detect_dialect(path)
    rows = read_rows(path, d)
    assert len(rows) == 1
    assert rows[0]["Balance"] == ""
