"""Generate synthetic statement fixtures.

No real financial data is in this repository and none should ever be. Names,
account numbers and merchants here are invented; when modelling a fixture on a
real statement's *layout*, copy the structure and never the identifiers. These
fixtures are produced from a seeded RNG, so the output is byte-stable across
runs and machines, and the generated files are committed — a fresh clone can
run the tests without executing this script.

Two things are deliberate here:

* **Every awkward real-world case is represented on purpose**: European decimal
  commas, ``windows-1252`` bytes, a preamble above the header, a running-balance
  column sitting next to the amount, unsigned amounts with a DR/CR type column,
  parenthesised negatives, and a file whose dates are genuinely ambiguous.
* **Subscriptions are planted with known cadence, amount and end date**, so the
  detector's tests assert against ground truth rather than against whatever the
  detector happened to output when the test was written.

Regenerate with::

    python tests/fixtures/generate.py
"""

from __future__ import annotations

import csv
import random
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20260820

# The fixture world runs Jan 2024 - Jun 2026; tests judge "is it still active?"
# as at AS_OF, which is a constant rather than today() so results never drift.
START = date(2024, 1, 1)
END = date(2026, 6, 30)
AS_OF = date(2026, 7, 15)

# --- ground truth the tests assert against ---------------------------------
PLANTED_SUBSCRIPTIONS = {
    "Netflix": {"cadence": "monthly", "amount": 15.99, "status": "active"},
    "Spotify": {"cadence": "monthly", "amount": 11.99, "status": "active",
                "price_changed": True},
    "Planet Fitness": {"cadence": "monthly", "amount": 24.99,
                       "status": "likely_cancelled"},
    "Amazon Web Services": {"cadence": "monthly", "status": "active",
                            "amount_varies": True},
    "GitHub": {"cadence": "annual", "amount": 84.00, "status": "active"},
}

NOISE_MERCHANTS = [
    ("SQ *BLUE BOTTLE #4412 OAKLAND CA", 4.50, 9.75),
    ("TST* SWEETGREEN 0184 NEW YORK NY", 12.00, 19.50),
    ("WHOLEFDS MKT #10238 OAKLAND CA", 22.00, 148.00),
    ("TRADER JOE'S #182 BERKELEY CA", 18.00, 96.00),
    ("AMZN Mktp US*2H4XY9DK3 AMZN.COM/BILL WA", 8.99, 210.00),
    ("SHELL OIL 57442890 SAN JOSE CA", 32.00, 78.00),
    ("UBER *TRIP HELP.UBER.COM CA", 8.00, 43.00),
    ("POS DEBIT - CVS/PHARMACY #8871 OAKLAND CA", 6.25, 62.00),
    ("MCDONALD'S F1234 SAN JOSE CA", 5.49, 16.80),
    ("TARGET 00012345 EMERYVILLE CA", 15.00, 180.00),
    ("DOORDASH*CHIPOTLE SAN FRANCISCO CA", 14.00, 38.00),
    ("PG&E WEB ONLINE PAYMENT", 45.00, 190.00),
]


def add_months(anchor: date, months: int) -> date:
    """Add ``months`` to ``anchor``, clamping the day to the target month."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, monthrange(year, month)[1])
    return date(year, month, day)


def _shift_off_weekend(day: date, rng: random.Random) -> date:
    """Billing dates drift when they land on a weekend; mimic that."""
    if day.weekday() == 5:
        return day + timedelta(days=rng.choice([-1, 2]))
    if day.weekday() == 6:
        return day + timedelta(days=rng.choice([-2, 1]))
    return day


def build_ledger(rng: random.Random) -> list[tuple[date, str, float]]:
    """The canonical ledger, before any bank-specific rendering."""
    rows: list[tuple[date, str, float]] = []

    # --- planted recurring charges -------------------------------------
    anchor = date(2024, 1, 7)
    for i in range(30):  # Netflix, monthly, still running
        day = _shift_off_weekend(add_months(anchor, i), rng)
        if day <= END:
            rows.append((day, "NETFLIX.COM 866-579-7172 CA", -15.99))

    anchor = date(2024, 1, 22)
    for i in range(30):  # Spotify, monthly, price rise in Oct 2025
        day = _shift_off_weekend(add_months(anchor, i), rng)
        if day <= END:
            price = 10.99 if day < date(2025, 10, 1) else 11.99
            rows.append((day, "SPOTIFY USA NEW YORK NY", -price))

    anchor = date(2024, 1, 3)
    for i in range(25):  # Planet Fitness, monthly, cancelled after Jan 2026
        day = _shift_off_weekend(add_months(anchor, i), rng)
        if day <= date(2026, 1, 31):
            rows.append((day, "PLANET FITNESS CLUB FEES 800-1234567 NH", -24.99))

    anchor = date(2024, 1, 15)
    for i in range(30):  # AWS, monthly, usage-based so the amount moves
        day = add_months(anchor, i)
        if day <= END:
            rows.append((day, "AMAZON WEB SERVICES AWS.AMAZON.CO WA",
                         -round(rng.uniform(38.0, 96.0), 2)))

    for year in (2024, 2025, 2026):  # GitHub, annual
        day = _shift_off_weekend(date(year, 3, 15), rng)
        if day <= END:
            rows.append((day, "GITHUB.COM HTTPSGITHUB.C CA", -84.00))

    # --- income: positive, and must never be read as a subscription -----
    anchor = date(2024, 1, 15)
    for i in range(60):  # semi-monthly payroll
        day = _shift_off_weekend(anchor + timedelta(days=i * 15), rng)
        if day <= END:
            rows.append((day, "ACME CORP DIRECT DEP PAYROLL", 2841.66))

    # --- background noise ------------------------------------------------
    day = START
    while day <= END:
        for _ in range(rng.randint(0, 4)):
            desc, lo, hi = rng.choice(NOISE_MERCHANTS)
            rows.append((day, desc, -round(rng.uniform(lo, hi), 2)))
        day += timedelta(days=1)

    # Two identical charges on one day: the dedup logic must keep both.
    rows.append((date(2025, 6, 12), "SQ *BLUE BOTTLE #4412 OAKLAND CA", -4.50))
    rows.append((date(2025, 6, 12), "SQ *BLUE BOTTLE #4412 OAKLAND CA", -4.50))

    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


# --- per-bank renderers -----------------------------------------------------

def write_main_checking(rows: list[tuple[date, str, float]]) -> None:
    """US style: MM/DD/YYYY, one signed Amount, plus a Balance decoy column."""
    path = HERE / "main_checking.csv"
    balance = 4200.00
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Details", "Posting Date", "Description", "Amount", "Type", "Balance"])
        for day, desc, amount in rows:
            balance += amount
            w.writerow([
                "DEBIT" if amount < 0 else "CREDIT",
                day.strftime("%m/%d/%Y"),
                desc,
                f"{amount:.2f}",
                "ACH_DEBIT" if amount < 0 else "ACH_CREDIT",
                f"{balance:.2f}",
            ])


def write_uk_current(rows: list[tuple[date, str, float]]) -> None:
    """UK style: DD/MM/YYYY with separate Paid Out / Paid In columns."""
    path = HERE / "uk_current.csv"
    subset = [r for r in rows if date(2026, 1, 1) <= r[0] <= date(2026, 6, 30)]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Description", "Paid Out", "Paid In", "Balance"])
        balance = 1850.00
        for day, desc, amount in subset:
            balance += amount
            w.writerow([
                day.strftime("%d/%m/%Y"),
                desc,
                f"{-amount:.2f}" if amount < 0 else "",
                f"{amount:.2f}" if amount > 0 else "",
                f"{balance:.2f}",
            ])


def write_euro_semicolon(rows: list[tuple[date, str, float]]) -> None:
    """European style: semicolons, DD.MM.YYYY, comma decimals, cp1252 bytes."""
    path = HERE / "euro_semicolon.csv"
    subset = [r for r in rows if date(2026, 3, 1) <= r[0] <= date(2026, 5, 31)]
    lines = ["Buchungstag;Verwendungszweck;Betrag;Währung"]
    for day, desc, amount in subset:
        euro = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        lines.append(f"{day.strftime('%d.%m.%Y')};{desc};{euro};EUR")
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252", errors="replace"))


def write_amex_preamble(rows: list[tuple[date, str, float]]) -> None:
    """Card style: junk preamble, unsigned amounts, spend-only, no type column."""
    path = HERE / "amex_preamble.csv"
    subset = [r for r in rows if date(2026, 4, 1) <= r[0] <= date(2026, 6, 30) and r[2] < 0]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Prepared for"])
        w.writerow(["A SAMPLE CARDHOLDER"])
        w.writerow(["Account ending", "XXXX-XXXXX0-00000"])
        w.writerow([])
        w.writerow(["Date", "Description", "Amount"])
        for day, desc, amount in subset:
            w.writerow([day.strftime("%m/%d/%Y"), desc, f"{-amount:.2f}"])


def write_credit_union(rows: list[tuple[date, str, float]]) -> None:
    """Type-column style: unsigned amounts with DR/CR, parenthesised negatives."""
    path = HERE / "credit_union.csv"
    subset = [r for r in rows if date(2026, 2, 1) <= r[0] <= date(2026, 4, 30)]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Transaction Date", "Narrative", "Amount", "DR/CR"])
        for day, desc, amount in subset:
            cell = f"({abs(amount):,.2f})" if amount < 0 else f"{amount:,.2f}"
            w.writerow([day.strftime("%d-%b-%Y"), desc, cell, "DR" if amount < 0 else "CR"])


def write_ambiguous_dates() -> None:
    """Every date has both components <= 12: no reading can be ruled out."""
    path = HERE / "ambiguous_dates.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Description", "Amount"])
        for day, month, desc, amount in [
            (2, 3, "NETFLIX.COM 866-579-7172 CA", -15.99),
            (4, 5, "SPOTIFY USA NEW YORK NY", -11.99),
            (6, 7, "WHOLEFDS MKT #10238 OAKLAND CA", -64.20),
            (8, 9, "SHELL OIL 57442890 SAN JOSE CA", -41.10),
        ]:
            w.writerow([f"{month:02d}/{day:02d}/2026", desc, f"{amount:.2f}"])


def write_overlap(rows: list[tuple[date, str, float]]) -> None:
    """Two exports with an overlapping month, as people actually download them."""
    # Named the way a bank actually names downloads, so that the importer has
    # to recognise both as the same account for dedup to be exercised.
    windows = {
        "chase_checking_2026q1.csv": (date(2026, 1, 1), date(2026, 3, 31)),
        "chase_checking_2026q2.csv": (date(2026, 3, 1), date(2026, 5, 31)),
    }
    for name, (lo, hi) in windows.items():
        subset = [r for r in rows if lo <= r[0] <= hi]
        with (HERE / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Posting Date", "Description", "Amount"])
            for day, desc, amount in subset:
                w.writerow([day.strftime("%m/%d/%Y"), desc, f"{amount:.2f}"])


def main() -> None:
    rng = random.Random(SEED)
    rows = build_ledger(rng)
    write_main_checking(rows)
    write_uk_current(rows)
    write_euro_semicolon(rows)
    write_amex_preamble(rows)
    write_credit_union(rows)
    write_ambiguous_dates()
    write_overlap(rows)
    write_card_statement_pdf()
    write_bank_statement_pdf()
    write_scanned_pdf()
    write_travel_statement_pdf()
    write_travel_csv()
    write_travel_csv_no_fx()
    n_csv = len(list(HERE.glob('*.csv')))
    n_pdf = len(list(HERE.glob('*.pdf')))
    print(f'wrote {n_csv} CSV and {n_pdf} PDF fixtures from {len(rows)} ledger rows')




# ---------------------------------------------------------------------------
# PDF fixtures
# ---------------------------------------------------------------------------
#
# These mirror the structures found in real statements, because the real
# statement they were modelled on cannot live in a public repository:
#
# * A card statement's transactions are split across "Summary" and "Detail"
#   blocks, and the detail resumes on later pages under "Detail Continued".
#   A parser that treats "Summary" as a section to skip loses every charge
#   after it unless something reopens the section.
# * A card prints purchases POSITIVE and your payment NEGATIVE — the opposite
#   of a current account. Reading one as the other inverts every figure.
# * Issuers add metadata lines under each charge (a phone number, a city, a
#   category hint) that must not become part of the merchant name.
# * The statement states its own section totals, which lets an extraction be
#   checked arithmetically rather than eyeballed.
#
# reportlab's invariant mode is required: without it each run embeds a new
# timestamp and document id, and the committed fixtures would never match.

CARD_PAYMENTS = [
    ("08/03/26*", "Mobile Payment - Thank You", 20.12),
    ("08/20/26*", "Mobile Payment - Thank You", 305.00),
]
CARD_CREDITS = [("08/16/26", "AMAZON PRIME AMZN.COM WA", 0.57, "Seattle")]
CARD_CHARGES = [
    ("08/04/26", "NETFLIX.COM 866-579-7172 CA", 15.99, "Entertainment"),
    ("08/05/26", "SQ *BLUE BOTTLE #4412 OAKLAND CA", 6.25, "408-746-5966"),
    ("08/07/26", "SPOTIFY USA NEW YORK NY", 11.99, "Subscription"),
    ("08/09/26", "WHOLEFDS MKT #10238 OAKLAND CA", 84.31, "Grocery"),
    ("08/11/26", "UBER TRIP HELP.UBER.COM CA", 25.32, "4EWR3XF 94043"),
    ("08/14/26", "TST* SWEETGREEN 0184 NEW YORK NY", 18.40, "sweetgreen.com/rewards"),
    ("08/16/26", "AMZN Mktp US*2H4XY9DK3 AMZN.COM/BILL WA", 17.03, "Bill Supplies"),
    ("08/19/26", "SHELL OIL 57442890 SAN JOSE CA", 47.88, "+14152360599"),
    ("08/22/26", "TRADER JOE'S #182 BERKELEY CA", 62.40, "Grocery"),
    ("08/24/26", "PLANET FITNESS CLUB FEES 800-1234567 NH", 24.99, "Membership"),
    ("08/26/26", "MCDONALD'S F1234 SAN JOSE CA", 9.42, "Restaurant"),
    ("08/28/26", "TARGET 00012345 EMERYVILLE CA", 103.77, "530-541-5160"),
    ("08/30/26", "GITHUB.COM HTTPSGITHUB.C CA", 84.00, "Software"),
    ("09/01/26", "DOORDASH*CHIPOTLE SAN FRANCISCO CA", 31.15, "Restaurant"),
]


def _card_total(rows) -> float:
    return round(sum(r[2] for r in rows), 2)


def write_card_statement_pdf() -> None:
    """A card statement: summary/detail split, inverted signs, metadata lines."""
    from reportlab.pdfgen import canvas

    path = HERE / "card_statement.pdf"
    c = canvas.Canvas(str(path), pagesize=(648, 792), invariant=1)
    c.setTitle("Statement")

    x_date, x_desc, x_right = 50, 101, 560
    payments_total = _card_total(CARD_PAYMENTS) + _card_total(CARD_CREDITS)
    charges_total = _card_total(CARD_CHARGES)

    def header(page_no: int, pages: int) -> float:
        c.setFont("Helvetica", 8)
        c.drawString(58, 765, "A SAMPLE CARDHOLDER")
        c.drawString(262, 765, "Account Ending 0-00000")
        c.drawString(511, 762, f"p. {page_no}/{pages}")
        c.drawString(94, 748, "Closing Date 09/02/26")
        return 720.0

    def money(y: float, text: str) -> None:
        c.setFont("Helvetica", 8)
        c.drawRightString(x_right, y, text)

    def row(y: float, date: str, desc: str, amount: str, meta: str | None) -> float:
        c.setFont("Helvetica", 8)
        # The date and amount sit on a very slightly different baseline from the
        # description, exactly as they do in a real statement. A parser that
        # clusters words by vertical position with a tight tolerance splits every
        # transaction here into two rows.
        c.drawString(x_date, y - 1.1, date)
        c.drawString(x_desc, y, desc)
        c.drawRightString(x_right, y - 1.1, amount)
        y -= 11.4
        if meta:
            c.setFont("Helvetica", 7)
            c.drawString(x_desc, y, meta)
            y -= 12.1
        return y

    # ---- page 1: payments and credits, then the New Charges summary ----
    y = header(1, 2)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(58, y, "Payments and Credits")
    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Summary")
    y -= 14
    c.drawRightString(x_right, y, "Total")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Payments")
    money(y, f"-${_card_total(CARD_PAYMENTS):,.2f}")
    y -= 12
    c.drawString(50, y, "Credits")
    money(y, f"-${_card_total(CARD_CREDITS):,.2f}")
    y -= 12
    c.drawString(50, y, "Total Payments and Credits")
    money(y, f"-${payments_total:,.2f}")
    y -= 20

    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Detail")
    y -= 14
    c.setFont("Helvetica-Oblique", 7)
    c.drawString(101, y, "*Indicates posting date")
    y -= 14
    c.setFont("Helvetica-Bold", 8)
    c.drawString(50, y, "Payments")
    c.drawRightString(x_right, y, "Amount")
    y -= 14
    for txn_date, desc, amount in CARD_PAYMENTS:
        y = row(y, txn_date, desc, f"-${amount:,.2f}", None)

    c.setFont("Helvetica-Bold", 8)
    c.drawString(50, y, "Credits")
    c.drawRightString(x_right, y, "Amount")
    y -= 14
    for txn_date, desc, amount, meta in CARD_CREDITS:
        y = row(y, txn_date, desc, f"-${amount:,.2f}", meta)

    y -= 10
    c.setFont("Helvetica-Bold", 10)
    c.drawString(58, y, "New Charges")
    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Summary")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Total New Charges")
    money(y, f"${charges_total:,.2f}")
    y -= 20

    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Detail")
    y -= 14
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(x_right, y, "Amount")
    y -= 14
    split_at = 6
    for txn_date, desc, amount, meta in CARD_CHARGES[:split_at]:
        y = row(y, txn_date, desc, f"${amount:,.2f}", meta)
    c.setFont("Helvetica-Oblique", 7)
    c.drawString(479, 40, "Continued on next page")
    c.showPage()

    # ---- page 2: the detail resumes, then fees ----
    y = header(2, 2)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Detail Continued")
    y -= 14
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(x_right, y, "Amount")
    y -= 14
    for txn_date, desc, amount, meta in CARD_CHARGES[split_at:]:
        y = row(y, txn_date, desc, f"${amount:,.2f}", meta)

    y -= 10
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Fees")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Total Fees for this Period")
    money(y, "$0.00")
    y -= 24

    # A year-to-date block, whose totals cover other statements and must not be
    # mistaken for this one's.
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "2026 Fees and Interest Totals Year-to-Date")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(58, y, "Total Fees in 2026")
    money(y, "$142.00")
    y -= 12
    c.drawString(58, y, "Total Interest in 2026")
    money(y, "$0.00")
    y -= 24

    # An APR table: a row that carries both a date and money, but is not a
    # transaction, and whose date is not the first thing on the line.
    c.setFont("Helvetica-Bold", 8)
    c.drawString(236, y, "Transactions Dates Annual Balance Interest")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(54, y, "Purchases 07/31/2023 28.49% (v) $0.00 $0.00")
    y -= 12
    c.drawString(54, y, "Cash Advances 07/31/2023 28.74% (v) $0.00 $0.00")
    c.save()


def write_bank_statement_pdf() -> None:
    """A bank statement drawn as a ruled grid, with a running balance column."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    path = HERE / "bank_statement_ruled.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=letter, invariant=1, title="Statement")

    data = [["Date", "Description", "Paid Out", "Paid In", "Balance"]]
    balance = 2500.00
    entries = [
        ("01/03/2026", "ACME CORP DIRECT DEP PAYROLL", None, 2841.66),
        ("02/03/2026", "NETFLIX.COM 866-579-7172 CA", 15.99, None),
        ("05/03/2026", "WHOLEFDS MKT #10238 OAKLAND CA", 91.244, None),
        ("13/03/2026", "TFL TRAVEL CHARGE TFL.GOV.UK/CP", 18.50, None),
        ("17/03/2026", "SPOTIFY USA NEW YORK NY", 11.99, None),
        ("25/03/2026", "SHELL OIL 57442890 SAN JOSE CA", 52.10, None),
    ]
    for entry_date, desc, out, inn in entries:
        out = round(out, 2) if out else None
        balance += (inn or 0) - (out or 0)
        data.append([
            entry_date, desc,
            f"{out:,.2f}" if out else "",
            f"{inn:,.2f}" if inn else "",
            f"{balance:,.2f}",
        ])

    table = Table(data, colWidths=[62, 230, 58, 58, 62])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
    ]))

    doc.build([
        Paragraph("Statement of Account", styles["Heading2"]),
        Paragraph("Sort code 00-00-00 &bull; Account 12345678", styles["Normal"]),
        Spacer(1, 12),
        table,
    ])


def write_scanned_pdf() -> None:
    """A page with no text layer at all, as a scan or photo of a statement is."""
    from reportlab.pdfgen import canvas

    path = HERE / "scanned_no_text.pdf"
    c = canvas.Canvas(str(path), invariant=1)
    c.setTitle("Scan")
    # Draw shapes only: visually a page, but containing no extractable text.
    c.rect(72, 600, 468, 120, fill=0)
    for i in range(12):
        c.line(80, 590 - i * 18, 520, 590 - i * 18)
    c.save()


# ---------------------------------------------------------------------------
# Travel fixtures
# ---------------------------------------------------------------------------
#
# A card used abroad produces rows that look ordinary and mean something else:
# the billed amount is a conversion of a local-currency charge, with a fee added
# and a rate applied. These fixtures plant all of that so the tests can assert
# against known truth rather than against whatever the parser happened to do.
#
# Planted deliberately:
#   * Three trips in three currencies, each a dense run of dates.
#   * One charge converted at the till (dynamic currency conversion). Per
#     Mastercard's merchant guide, such a charge reaches the issuer ALREADY in
#     the cardholder's currency, so it carries NO local amount and NO rate —
#     it is the one foreign-looking row on the trip with nothing beneath it.
#     An earlier version of this fixture gave it a local amount and a bad rate,
#     which is not what a statement shows, and the tests happily confirmed the
#     misconception.
#   * Foreign transaction fees at a fixed percentage, posting a day or two
#     AFTER the charge they belong to. An earlier version posted them on the
#     same date, which is tidy and wrong: a statement's date column mixes
#     transaction and posting dates, and a purchase abroad already falls on a
#     different calendar day at home.
#   * A single foreign website order, which is NOT a trip and must not be
#     reported as one.
#   * A domestic charge ending "INDIANAPOLIS IN", which must stay in Indiana
#     rather than being read as India.

FEE_RATE = 0.027

TRIPS = [
    {
        "country": "BR", "currency": "BRL", "rate": 5.1400,
        "charges": [
            ("2026-09-12", "RESTAURANTE SABOR SAO PAULO BR", 125.00),
            ("2026-09-13", "HOTEL COPACABANA RIO DE JANEIRO BR", 840.00),
            ("2026-09-14", "UBER DO BRASIL SAO PAULO BR", 46.50),
            ("2026-09-16", "MERCADO MUNICIPAL SAO PAULO BR", 212.30),
            ("2026-09-18", "CAFE DO CENTRO RIO DE JANEIRO BR", 38.00),
            ("2026-09-20", "LIVRARIA CULTURA SAO PAULO BR", 96.80),
            ("2026-09-22", "AEROPORTO DUTY FREE SAO PAULO BR", 310.00),
        ],
        # Converted by the merchant's terminal: billed straight in USD, so the
        # issuer prints no local amount and no rate for it. Only the USD figure.
        "dcc": ("2026-09-19", "LOJA TURISTICA RIO DE JANEIRO BR", 84.21),
    },
    {
        "country": "MX", "currency": "MXN", "rate": 20.1000,
        "charges": [
            ("2026-11-05", "TAQUERIA EL SOL CIUDAD DE MEXICO MX", 480.00),
            ("2026-11-06", "HOTEL AZTECA CIUDAD DE MEXICO MX", 3200.00),
            ("2026-11-08", "MUSEO NACIONAL CIUDAD DE MEXICO MX", 95.00),
            ("2026-11-09", "MERCADO ROMA CIUDAD DE MEXICO MX", 640.00),
            ("2026-11-11", "AEROPUERTO CAFE CIUDAD DE MEXICO MX", 210.00),
        ],
        "dcc": None,
    },
    {
        "country": "IN", "currency": "INR", "rate": 83.4000,
        "charges": [
            ("2026-12-02", "CAFE COFFEE DAY MUMBAI IN", 1250.00),
            ("2026-12-03", "TAJ HOTELS MUMBAI IN", 18500.00),
            ("2026-12-05", "OLA CABS BENGALURU IN", 640.00),
            ("2026-12-07", "FABINDIA BENGALURU IN", 4300.00),
            ("2026-12-09", "INDIAN COFFEE HOUSE DELHI IN", 380.00),
            ("2026-12-12", "KHAN MARKET DELHI IN", 2750.00),
        ],
        "dcc": None,
    },
]

# Foreign, but not travel: one order from a British website.
ONLINE_FOREIGN = [("2026-10-14", "BOOKSHOP ONLINE LONDON GB", 42.00, "GBP", 0.7900)]

DOMESTIC = [
    ("2026-09-02", "WHOLEFDS MKT #10238 OAKLAND CA", 84.31),
    ("2026-09-28", "INDIANAPOLIS COLTS SHOP INDIANAPOLIS IN", 45.00),
    ("2026-10-03", "NETFLIX.COM 866-579-7172 CA", 15.99),
    ("2026-10-21", "SHELL OIL 57442890 SAN JOSE CA", 52.10),
    ("2026-11-25", "TRADER JOE'S #182 BERKELEY CA", 62.40),
    ("2026-12-20", "TARGET 00012345 EMERYVILLE CA", 103.77),
]


def _billed(local: float, rate: float) -> float:
    return round(local / rate, 2)


def _shift(day: str, days: int) -> str:
    """Move an ISO date on, for fees that post after the charge they belong to."""
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def travel_rows() -> list[tuple]:
    """(date, descriptor, local_amount, currency, rate, billed, is_fee)."""
    out: list[tuple] = []
    for trip in TRIPS:
        for i, (day, desc, local) in enumerate(trip["charges"]):
            billed = _billed(local, trip["rate"])
            out.append((day, desc, local, trip["currency"], trip["rate"], billed, False))
            out.append((_shift(day, 1 + i % 2), "FOREIGN TRANSACTION FEE",
                        None, None, None, round(billed * FEE_RATE, 2), True))
        if trip["dcc"]:
            # No local amount, no currency, no rate: the issuer never saw a
            # foreign-currency transaction. The fee is still charged, because
            # the issuer goes by where the charge was processed.
            day, desc, billed = trip["dcc"]
            out.append((day, desc, None, None, None, billed, False))
            out.append((_shift(day, 2), "FOREIGN TRANSACTION FEE", None, None, None,
                        round(billed * FEE_RATE, 2), True))
    for day, desc, local, code, rate in ONLINE_FOREIGN:
        billed = _billed(local, rate)
        out.append((day, desc, local, code, rate, billed, False))
    for day, desc, billed in DOMESTIC:
        out.append((day, desc, None, None, None, billed, False))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def write_travel_statement_pdf() -> None:
    """A card statement from abroad: FX detail on continuation lines."""
    from reportlab.pdfgen import canvas

    path = HERE / "travel_statement.pdf"
    c = canvas.Canvas(str(path), pagesize=(648, 792), invariant=1)
    c.setTitle("Statement")
    rows = travel_rows()
    total = round(sum(r[5] for r in rows), 2)

    x_date, x_desc, x_right = 50, 101, 560
    page, y = 1, 0.0

    def header() -> float:
        c.setFont("Helvetica", 8)
        c.drawString(58, 765, "A SAMPLE CARDHOLDER")
        c.drawString(262, 765, "Account Ending 0-00000")
        c.drawString(511, 762, f"p. {page}")
        return 720.0

    y = header()
    c.setFont("Helvetica-Bold", 10)
    c.drawString(58, y, "New Charges")
    y -= 18
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Summary")
    y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(50, y, "Total New Charges")
    c.drawRightString(x_right, y, f"${total:,.2f}")
    y -= 20
    c.setFont("Helvetica-Bold", 9)
    c.drawString(58, y, "Detail")
    y -= 14
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(x_right, y, "Amount")
    y -= 14

    for day, desc, local, code, rate, billed, _is_fee in rows:
        if y < 90:
            c.setFont("Helvetica-Oblique", 7)
            c.drawString(479, 40, "Continued on next page")
            c.showPage()
            page += 1
            y = header()
            c.setFont("Helvetica-Bold", 9)
            c.drawString(58, y, "Detail Continued")
            y -= 14
            c.setFont("Helvetica-Bold", 8)
            c.drawRightString(x_right, y, "Amount")
            y -= 14
        shown = f"{day[5:7]}/{day[8:10]}/{day[2:4]}"
        c.setFont("Helvetica", 8)
        c.drawString(x_date, y - 1.1, shown)
        c.drawString(x_desc, y, desc)
        c.drawRightString(x_right, y - 1.1, f"${billed:,.2f}")
        y -= 11.4
        if local is not None and code:
            c.setFont("Helvetica", 7)
            c.drawString(x_desc, y, f"{local:,.2f} {code}")
            y -= 10.0
            c.drawString(x_desc, y, f"Exchange Rate {rate:.6f}")
            y -= 12.1
    c.save()


def write_travel_csv() -> None:
    """The same trips as a CSV export, with the FX detail inside the descriptor."""
    path = HERE / "travel_card_2026q4.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Transaction Date", "Description", "Amount", "Currency"])
        for day, desc, local, code, _rate, billed, _is_fee in travel_rows():
            text = f"{desc} {local:,.2f} {code}" if local is not None and code else desc
            w.writerow([f"{day[5:7]}/{day[8:10]}/{day[:4]}", text, f"-{billed:.2f}", "USD"])


def write_travel_csv_no_fx() -> None:
    """The common case: a CSV export with the FX detail stripped out.

    Most card CSV exports give only the converted home-currency figure — no
    original amount, no currency code, no rate. Country can still be read off
    the descriptor, so trips are still found, but conversion quality cannot be
    judged at all. This fixture exists so the tests can prove the tool says so
    rather than reporting a reassuring zero.
    """
    path = HERE / "travel_card_no_fx.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Transaction Date", "Description", "Amount"])
        for day, desc, _local, _code, _rate, billed, _is_fee in travel_rows():
            w.writerow([f"{day[5:7]}/{day[8:10]}/{day[:4]}", desc, f"-{billed:.2f}"])


if __name__ == "__main__":
    main()
