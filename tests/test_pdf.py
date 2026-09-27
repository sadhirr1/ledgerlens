"""Reading transactions out of statement PDFs.

The two tests that matter most here are :func:`test_charges_after_the_summary_block_are_not_lost`
and :func:`test_a_card_payment_is_not_recorded_as_spending`. Both cover bugs that
were present in the first working version of this module and that produce
confidently wrong output rather than an error — the first silently drops most of
a statement, the second inverts every figure on it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ledgerlens.config import DialectOverride
from ledgerlens.ingest import import_file
from ledgerlens.ingest.pdf import PdfExtractionError, extract


def _amount(raw: str) -> Decimal:
    return Decimal(raw.replace("$", "").replace(",", "").replace("-", "").strip())


@pytest.fixture()
def card(fixtures):
    return extract(fixtures / "card_statement.pdf")


@pytest.fixture()
def ruled(fixtures):
    return extract(fixtures / "bank_statement_ruled.pdf")


# --- the card statement, via text layout -----------------------------------

def test_card_statement_is_recognised(card):
    assert card.method == "pdf_text_layout"
    assert card.statement_kind == "credit_card"
    assert card.pre_signed is True
    assert {"payments", "credits", "charges"} <= set(card.sections_seen)


def test_charges_after_the_summary_block_are_not_lost(card, spec):
    """Regression: a statement is laid out as Section / Summary / Detail.

    Treating "Summary" as a section to skip is correct, but the skip has to be
    released again — the detail rows come after it, and on later pages under
    "Detail Continued". The first version of this module latched the skip on and
    silently returned 4 rows out of 17.
    """
    charges = [r for r in card.rows if r["Section"] == "charges"]
    assert len(charges) == len(spec.CARD_CHARGES), (
        "every charge in the Detail block should be found, including the ones on "
        "the continuation page"
    )


def test_detail_continues_across_a_page_break(card, spec):
    dates = [r["Date"] for r in card.rows if r["Section"] == "charges"]
    assert dates[0] == spec.CARD_CHARGES[0][0]
    assert dates[-1] == spec.CARD_CHARGES[-1][0], "the last page's rows are missing"


def test_a_card_payment_is_not_recorded_as_spending(card):
    """Regression: a card inverts the sign convention a bank account uses.

    A purchase prints positive and your monthly payment prints negative — the
    opposite of a current account. The section heading states the direction, and
    the printed sign restates it; applying both inverts everything.
    """
    payments = [r for r in card.rows if r["Section"] == "payments"]
    assert payments
    assert all(not r["Amount"].startswith("-") for r in payments), (
        "paying your card bill is money arriving at the card account, not spending"
    )

    charges = [r for r in card.rows if r["Section"] == "charges"]
    assert all(r["Amount"].startswith("-") for r in charges), (
        "a purchase on a card is money out"
    )


def test_refund_is_money_in(card):
    credits = [r for r in card.rows if r["Section"] == "credits"]
    assert credits and all(not r["Amount"].startswith("-") for r in credits)


def test_extraction_reconciles_against_the_statements_own_totals(card, spec):
    """The strongest available check: does our arithmetic match the issuer's?"""
    assert card.reconciled is True, card.reconciliation
    assert card.confidence >= 0.95

    charges_total = sum(_amount(r["Amount"]) for r in card.rows if r["Section"] == "charges")
    assert charges_total == Decimal(str(spec._card_total(spec.CARD_CHARGES)))


def test_summary_rows_are_not_imported_as_transactions(card):
    """"Total Payments and Credits -$325.69" is a total, not a transaction."""
    assert not any("Total" in r["Description"] for r in card.rows)
    assert all(_amount(r["Amount"]) < Decimal("400") for r in card.rows)


def test_the_apr_table_is_not_mistaken_for_transactions(card):
    """A rate table row carries a date and two amounts but is not a transaction.

    It survives a money-column test and a date test; what excludes it is that its
    date is not the first thing on the line.
    """
    assert not any("28.49" in r["Description"] for r in card.rows)
    assert not any(r["Date"].startswith("07/31") for r in card.rows)


def test_year_to_date_totals_do_not_corrupt_reconciliation(card):
    """"Total Fees in 2026" covers other statements and must be ignored."""
    assert card.reconciled is True
    assert "142" not in card.reconciliation.get("out", "")


def test_metadata_lines_do_not_become_merchant_names(card):
    """Issuers add a phone number under each charge; it is not part of the name."""
    joined = " ".join(r["Description"] for r in card.rows)
    assert "530-541-5160" not in joined
    assert "+14152360599" not in joined
    assert "408-746-5966" not in joined


def test_posting_date_asterisk_is_stripped(card):
    assert all("*" not in r["Date"] for r in card.rows)


# --- the ruled bank statement, via table extraction ------------------------

def test_ruled_table_uses_the_grid(ruled):
    assert ruled.method == "pdf_ruled_table"
    assert ruled.columns["debit"] == "Paid Out"
    assert ruled.columns["credit"] == "Paid In"


def test_balance_column_is_excluded_from_the_ruled_table(ruled):
    assert "Balance" not in ruled.columns.values(), (
        "a running-balance column would invert every total if read as an amount"
    )


# --- failure modes ---------------------------------------------------------

def test_a_scan_says_so_and_suggests_ocr(fixtures):
    with pytest.raises(PdfExtractionError) as exc:
        extract(fixtures / "scanned_no_text.pdf")
    message = str(exc.value).lower()
    assert "scan" in message or "image" in message
    assert "ocr" in message, "the error should say how to fix it"


def test_a_password_protected_pdf_explains_itself(fixtures, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    locked = tmp_path / "locked.pdf"
    reader = pypdf.PdfReader(str(fixtures / "card_statement.pdf"))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    with locked.open("wb") as fh:
        writer.write(fh)

    with pytest.raises(PdfExtractionError) as exc:
        extract(locked)
    assert "password" in str(exc.value).lower()
    assert "ledgerlens.yaml" in str(exc.value)

    ok = extract(locked, password="hunter2")
    assert len(ok.rows) > 10, "the right password should just work"


def test_a_pdf_that_is_not_a_statement_fails_clearly(tmp_path):
    from reportlab.pdfgen import canvas

    path = tmp_path / "essay.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "This document contains prose and no transactions.")
    c.save()

    with pytest.raises(PdfExtractionError) as exc:
        extract(path)
    assert "csv" in str(exc.value).lower(), "point the user at the reliable path"


# --- end to end through the importer --------------------------------------

def test_importing_a_card_pdf(conn, fixtures, spec):
    result = import_file(conn, fixtures / "card_statement.pdf")
    assert not result.errors, result.errors
    assert result.rows_inserted == len(card_rows := spec.CARD_CHARGES) + len(
        spec.CARD_PAYMENTS
    ) + len(spec.CARD_CREDITS)
    assert card_rows  # silence the walrus lint
    assert any("reconcile" in note for note in result.notes)


def test_pdf_import_gets_the_direction_right_in_the_database(conn, fixtures):
    import_file(conn, fixtures / "card_statement.pdf")

    netflix = conn.execute(
        "SELECT amount_cents FROM transactions WHERE merchant = 'Netflix'"
    ).fetchall()
    assert netflix and all(r["amount_cents"] == -1599 for r in netflix)

    payment = conn.execute(
        "SELECT amount_cents FROM transactions WHERE raw_description LIKE '%Mobile Payment%'"
    ).fetchall()
    assert payment and all(r["amount_cents"] > 0 for r in payment), (
        "a bill payment must not show up as spending"
    )


def test_pdf_merchants_normalize_like_csv_ones(conn, fixtures):
    import_file(conn, fixtures / "card_statement.pdf")
    merchants = {
        r["merchant"]
        for r in conn.execute("SELECT DISTINCT merchant FROM transactions").fetchall()
    }
    assert {"Netflix", "Spotify", "Whole Foods", "Blue Bottle", "GitHub"} <= merchants


def test_pdf_import_is_idempotent(conn, fixtures):
    first = import_file(conn, fixtures / "card_statement.pdf")
    again = import_file(conn, fixtures / "card_statement.pdf")
    assert again.already_imported
    total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    assert total == first.rows_inserted


def test_ruled_pdf_import_resolves_day_first_dates(conn, fixtures):
    """The ruled fixture uses DD/MM/YYYY; a month-first read would scatter it."""
    result = import_file(conn, fixtures / "bank_statement_ruled.pdf")
    assert result.rows_inserted == 6, result.errors
    months = {
        r["posted_on"][:7]
        for r in conn.execute("SELECT posted_on FROM transactions").fetchall()
    }
    assert months == {"2026-03"}, f"all six rows are in March; got {months}"


def test_pdf_password_from_the_folder_override(conn, fixtures, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(str(fixtures / "card_statement.pdf"))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("s3cret")
    target = tmp_path / "amex_statement.pdf"
    with target.open("wb") as fh:
        writer.write(fh)

    blocked = import_file(conn, target)
    assert blocked.errors and "password" in blocked.errors[0].lower()

    ok = import_file(
        conn, target, override=DialectOverride(pdf_password="s3cret"), force=True
    )
    assert ok.rows_inserted > 10, ok.errors


def test_mixed_folder_of_csv_and_pdf(conn, fixtures):
    from ledgerlens.ingest import import_folder

    results = import_folder(conn, fixtures)
    suffixes = {r.path.rsplit(".", 1)[-1] for r in results}
    assert suffixes == {"csv", "pdf"}
    assert sum(r.rows_inserted for r in results) > 2000
    # The unreadable scan should report an error without derailing the folder.
    scanned = [r for r in results if r.path.endswith("scanned_no_text.pdf")]
    assert scanned and scanned[0].errors
