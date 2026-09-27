"""End-to-end import behaviour.

The assertions that matter most here are about *direction* and *duplication*.
An inverted sign makes every report wrong while looking plausible, and a double
import inflates spending by exactly the amount of the overlap — both are silent
failures, which is why they get the most coverage.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from ledgerlens.db import date_range, transaction_count
from ledgerlens.ingest import import_file, import_folder, recategorize
from ledgerlens.ingest.loader import _account_name_from_path


def _spend_for(conn, merchant: str) -> list[int]:
    rows = conn.execute(
        "SELECT amount_cents FROM transactions WHERE merchant = ? ORDER BY posted_on",
        (merchant,),
    ).fetchall()
    return [r["amount_cents"] for r in rows]


# --- direction, across every dialect ---------------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "main_checking.csv",     # signed amount column
        "uk_current.csv",        # split debit/credit columns
        "euro_semicolon.csv",    # signed, European formatting
        "credit_union.csv",      # unsigned + DR/CR type column
        "amex_preamble.csv",     # unsigned, spend-only
    ],
)
def test_spending_is_negative_in_every_dialect(conn, fixtures, filename):
    result = import_file(conn, fixtures / filename)
    assert result.rows_inserted > 0, result.errors[:3]
    netflix = _spend_for(conn, "Netflix")
    assert netflix, f"{filename}: Netflix charges should have been imported"
    assert all(c < 0 for c in netflix), f"{filename}: spending must be negative"
    assert all(abs(c) == 1599 for c in netflix), f"{filename}: amount was mangled"


def test_income_is_positive(conn, fixtures):
    import_file(conn, fixtures / "main_checking.csv")
    payroll = _spend_for(conn, "Acme Corp Direct Dep Payroll")
    assert payroll and all(c > 0 for c in payroll)
    assert all(c == 284166 for c in payroll)


def test_uk_debit_credit_columns_resolve_both_directions(conn, fixtures):
    import_file(conn, fixtures / "uk_current.csv")
    row = conn.execute(
        "SELECT SUM(CASE WHEN amount_cents > 0 THEN 1 ELSE 0 END) AS inflows,"
        "       SUM(CASE WHEN amount_cents < 0 THEN 1 ELSE 0 END) AS outflows"
        " FROM transactions"
    ).fetchone()
    assert row["inflows"] > 0 and row["outflows"] > 0


def test_european_decimal_comma_is_read_correctly(conn, fixtures):
    import_file(conn, fixtures / "euro_semicolon.csv")
    # "-15,99" must be 1599 cents, not 1599 euros or 15 cents.
    assert all(abs(c) == 1599 for c in _spend_for(conn, "Netflix"))


def test_parenthesised_negatives_are_outflows(conn, fixtures):
    import_file(conn, fixtures / "credit_union.csv")
    assert all(c < 0 for c in _spend_for(conn, "Netflix"))


def test_dates_land_in_the_right_months(conn, fixtures):
    """The UK fixture is day-first; a month-first read would scatter it."""
    import_file(conn, fixtures / "uk_current.csv")
    lo, hi = date_range(conn)
    assert date.fromisoformat(lo) >= date(2026, 1, 1)
    assert date.fromisoformat(hi) <= date(2026, 6, 30)


# --- duplication -----------------------------------------------------------

def test_reimporting_the_same_file_is_a_no_op(conn, fixtures):
    first = import_file(conn, fixtures / "main_checking.csv")
    total = transaction_count(conn)
    second = import_file(conn, fixtures / "main_checking.csv")
    assert second.already_imported
    assert transaction_count(conn) == total
    assert first.rows_inserted > 0


def test_forced_reimport_still_does_not_duplicate(conn, fixtures):
    import_file(conn, fixtures / "main_checking.csv")
    total = transaction_count(conn)
    again = import_file(conn, fixtures / "main_checking.csv", force=True)
    assert not again.already_imported, "force should actually re-read the file"
    assert transaction_count(conn) == total, "content hashing must still dedupe"


def test_overlapping_exports_do_not_double_count(conn, fixtures, spec):
    """Two downloads sharing March must yield the union, not the sum."""
    import_file(conn, fixtures / "chase_checking_2026q1.csv")
    after_first = transaction_count(conn)
    import_file(conn, fixtures / "chase_checking_2026q2.csv")
    after_second = transaction_count(conn)

    ledger = spec.build_ledger(random.Random(spec.SEED))
    expected = len([r for r in ledger if date(2026, 1, 1) <= r[0] <= date(2026, 5, 31)])

    assert after_second == expected, "March was counted twice or lost"
    assert after_second < after_first * 2


def test_both_exports_map_to_one_account(conn, fixtures):
    import_file(conn, fixtures / "chase_checking_2026q1.csv")
    import_file(conn, fixtures / "chase_checking_2026q2.csv")
    names = [r["name"] for r in conn.execute("SELECT name FROM accounts").fetchall()]
    assert names == ["Chase Checking"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("chase_checking_2026q1.csv", "Chase Checking"),
        ("chase_checking_2026q2.csv", "Chase Checking"),
        ("amex-gold-2026-03.csv", "Amex Gold"),
        ("barclays_statement_jan.csv", "Barclays"),
        ("savings.csv", "Savings"),
    ],
)
def test_account_names_ignore_period_markers(tmp_path, filename, expected):
    assert _account_name_from_path(tmp_path / filename) == expected


def test_two_identical_charges_on_one_day_both_survive(conn, fixtures):
    """Two $4.50 coffees on the same day are not a duplicate of each other."""
    import_file(conn, fixtures / "main_checking.csv")
    rows = conn.execute(
        """
        SELECT COUNT(*) AS n FROM transactions
        WHERE posted_on = '2025-06-12' AND merchant = 'Blue Bottle' AND amount_cents = -450
        """
    ).fetchone()
    assert rows["n"] == 2


# --- reporting and robustness ----------------------------------------------

def test_low_confidence_decisions_are_surfaced_as_warnings(conn, fixtures):
    result = import_file(conn, fixtures / "ambiguous_dates.csv")
    assert any("ambiguous" in w.lower() for w in result.warnings), (
        "a guess this consequential must be reported, not made silently"
    )


def test_unsigned_spend_only_export_warns_about_the_assumption(conn, fixtures):
    result = import_file(conn, fixtures / "amex_preamble.csv")
    assert any("direction" in w.lower() for w in result.warnings)


def test_import_folder_reads_every_statement(conn, fixtures):
    from ledgerlens.ingest.loader import DATA_SUFFIXES

    expected = [
        p
        for p in fixtures.iterdir()
        if p.is_file() and p.suffix.lower() in DATA_SUFFIXES and p.name != "ledgerlens.yaml"
    ]
    results = import_folder(conn, fixtures)
    assert len(results) == len(expected)
    assert transaction_count(conn) > 2000


def test_source_files_are_never_modified(conn, fixtures):
    target = fixtures / "main_checking.csv"
    before = target.read_bytes()
    import_file(conn, target)
    assert target.read_bytes() == before


def test_categories_are_applied_on_import(conn, fixtures):
    import_file(conn, fixtures / "main_checking.csv")
    row = conn.execute(
        "SELECT category FROM transactions WHERE merchant = 'Netflix' LIMIT 1"
    ).fetchone()
    assert row["category"] == "Subscriptions"


def test_recategorize_updates_existing_rows(conn, fixtures, home):
    import_file(conn, fixtures / "main_checking.csv")
    conn.execute("UPDATE transactions SET category = 'Uncategorized'")
    conn.commit()
    stats = recategorize(conn)
    assert stats["changed"] > 0
    row = conn.execute(
        "SELECT category FROM transactions WHERE merchant = 'Netflix' LIMIT 1"
    ).fetchone()
    assert row["category"] == "Subscriptions"


def test_unreadable_file_reports_an_error_without_raising(conn, tmp_path):
    junk = tmp_path / "notes.csv"
    junk.write_text("this is not a statement\n", encoding="utf-8")
    result = import_file(conn, junk)
    assert result.errors and result.rows_inserted == 0


def test_import_log_records_what_was_decided(conn, fixtures):
    import_file(conn, fixtures / "main_checking.csv")
    row = conn.execute("SELECT dialect_json FROM import_log").fetchone()
    assert "date_plan" in row["dialect_json"]
    assert "sign_plan" in row["dialect_json"]
