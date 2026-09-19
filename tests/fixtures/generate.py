"""Generate synthetic statement fixtures.

No real financial data is in this repository and none should ever be. These
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
        w.writerow(["A SAMPLE"])
        w.writerow(["Account ending", "XXXX-XXXXX1-00000"])
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
    print(f"wrote {len(list(HERE.glob('*.csv')))} fixtures from {len(rows)} ledger rows")


if __name__ == "__main__":
    main()
