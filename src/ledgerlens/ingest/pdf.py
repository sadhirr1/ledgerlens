"""Reading transactions out of statement PDFs.

A PDF is not a table. It records "draw the text ``$17.03`` at x=531, y=138" and
nothing else — the columns you see are an artifact of where the ink landed. This
module reconstructs the table from those positions.

Three problems have to be solved, and they are what makes this harder than the
CSV path:

**Which rows are transactions.** A statement is full of numbers that are not
transactions: a summary block totalling the section below it, a fees line, an
APR table, the closing date in the page header. Requiring that a row *begin*
with a date and carry an amount in the right-hand money column eliminates almost
all of them — ``Total Payments and Credits -$340.68`` has no date, and
``Purchases 07/31/2023 28.49% (v) $0.00`` has one but not in first position.

**Which direction the money went.** This is the part a naive reader gets
backwards, because a credit card statement inverts the convention a bank
statement uses. On a card, a purchase is printed as *positive* and your monthly
payment as *negative* — the opposite of a current account. The truth lives in
the section headings ("New Charges", "Payments and Credits"), so this module
tracks section state as it walks the document and normalizes the sign itself
rather than leaving a later stage to guess from the numbers alone.

**Where a row ends.** Statements wrap long descriptions, and issuers add extra
lines of merchant metadata — a phone number, a city, a category hint — beneath
each charge. Those attach to the transaction above them; phone numbers and URLs
are dropped rather than glued into merchant names.

Row grouping is delegated to pdfplumber's ``extract_text_lines()``. Clustering
words by vertical position by hand looks easy and is not: the date and the
amount on one row are often set in a different font from the description, so
their baselines differ by around a point, and a naive tolerance splits every
transaction in the document into two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Column names this module emits. The downstream pipeline treats the extracted
# rows exactly as though they had come from a CSV with these headers.
COL_DATE = "Date"
COL_DESC = "Description"
COL_AMOUNT = "Amount"
COL_SECTION = "Section"
COL_FOREIGN = "ForeignDetail"

# A date at the very start of a row. The trailing asterisk is Amex's
# posting-date marker.
_LEADING_DATE = re.compile(
    r"""^\s*(
          \d{4}-\d{1,2}-\d{1,2}
        | \d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?
        | \d{1,2}\s+[A-Z][a-z]{2}[a-z]*(?:\s+\d{2,4})?
        | [A-Z][a-z]{2}[a-z]*\s+\d{1,2}(?:,?\s+\d{2,4})?
    )\*?\s+""",
    re.VERBOSE,
)

# Money always carries cents on a statement, which is what separates it from
# reference numbers, store numbers and zip codes. A trailing % is a rate.
_MONEY = re.compile(
    r"""(?<![\w.])
        (?P<body>
            [-+]?\s?[$£€]?\s?\(?
            \d{1,3}(?:,\d{3})*\.\d{2}
            \)?\s?(?:-|CR|DR)?
        )
        (?!\s*%)(?![\d])""",
    re.IGNORECASE | re.VERBOSE,
)

# Section headings, and which way money flows inside them. Order matters: the
# more specific pattern has to be tested first.
_SECTION_SIGNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"payments?\s+and\s+credits", re.I), +1, "payments_credits"),
    (re.compile(r"^\s*payments?\b", re.I), +1, "payments"),
    (re.compile(r"^\s*credits?\b|refunds?\b", re.I), +1, "credits"),
    (re.compile(r"new\s+charges|^\s*charges\b|purchases?\s+and\s+adjustments", re.I), -1, "charges"),
    (re.compile(r"^\s*fees?\b|fees\s+charged", re.I), -1, "fees"),
    (re.compile(r"interest\s+charged", re.I), -1, "interest"),
    (re.compile(r"deposits|money\s+in|amounts?\s+received|paid\s+in", re.I), +1, "deposits"),
    (re.compile(r"withdrawals|money\s+out|paid\s+out|checks?\s+paid", re.I), -1, "withdrawals"),
]

# Headings that introduce numbers which are not transactions.
_SKIP_SECTION = re.compile(
    r"summary|year[-\s]?to[-\s]?date|annual\s+percentage|"
    r"rate\s+information|important|how\s+to\s+avoid|minimum\s+payment\s+warning",
    re.I,
)

# Statements are organised as "<Section> / Summary / Detail", and the detail can
# resume on a later page under "Detail Continued". Without an explicit release,
# the skip that Summary starts would swallow every transaction that follows it.
_DETAIL_HEADING = re.compile(r"\bdetail\b", re.I)

# Rows that survive the date test but are still not transactions.
_NOT_A_TRANSACTION = re.compile(
    r"^\s*(total|subtotal|balance|previous\s+balance|closing|statement|"
    r"continued|page\b|amount\s+due|minimum)",
    re.I,
)

# Continuation lines worth keeping out of a merchant name.
_PURE_METADATA = re.compile(
    r"^\s*(?:"
    r"\+?\d[\d\s()-]{6,}"                      # phone number
    r"|(?:https?://|www\.)\S+"                 # url
    r"|\S+@\S+"                                # email
    r"|\d{5}(?:-\d{4})?"                       # zip
    r"|[\d\s/,-]+"                             # bare numbers
    r")\s*$"
)

MAX_CONTINUATION_LINES = 2
_MONEY_COLUMN_FRACTION = 0.62  # amounts live in the right-hand third of the page

# A statement states its own section totals. Summing the rows we extracted and
# comparing against those figures turns "this probably parsed correctly" into
# something checkable — see :func:`_reconcile`.
_TOTAL_LINE = re.compile(r"^\s*total\b", re.I)
# "Total Fees in 2026" is a year-to-date figure covering other statements; only
# this period's totals are comparable to the rows on this statement.
_YTD_TOTAL = re.compile(r"year[-\s]?to[-\s]?date|\bin\s*20\d{2}\b", re.I)
_RECONCILE_TOLERANCE = Decimal("0.02")


class PdfExtractionError(ValueError):
    """Raised when a PDF cannot be read as a statement."""


@dataclass
class PdfExtraction:
    """Transactions recovered from a PDF, shaped like parsed CSV rows."""

    rows: list[dict[str, str]]
    columns: dict[str, str]
    method: str
    confidence: float
    pages: int
    statement_kind: str = "unknown"
    pre_signed: bool = False
    warnings: list[str] = field(default_factory=list)
    sections_seen: list[str] = field(default_factory=list)
    reconciled: bool | None = None
    reconciliation: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "confidence": round(self.confidence, 2),
            "pages": self.pages,
            "statement_kind": self.statement_kind,
            "rows_found": len(self.rows),
            "sections": self.sections_seen,
            "reconciled": self.reconciled,
            "reconciliation": self.reconciliation,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _money_tokens(text: str) -> list[tuple[str, int, int]]:
    """Money-shaped substrings of ``text`` as (token, start, end)."""
    return [(m.group("body").strip(), m.start(), m.end()) for m in _MONEY.finditer(text)]


def _is_metadata_line(text: str) -> bool:
    return bool(_PURE_METADATA.match(text.strip()))


def _classify_heading(text: str) -> tuple[int | None, str | None, bool]:
    """Return (sign, section name, is_skip) for a heading candidate."""
    stripped = text.strip()
    if _SKIP_SECTION.search(stripped):
        return None, None, True
    for pattern, sign, name in _SECTION_SIGNS:
        if pattern.search(stripped):
            return sign, name, False
    return None, None, False


def _looks_like_heading(text: str) -> bool:
    """Distinguish a section heading from a sentence of statement small print.

    Pages of legal text mention "payments" and "credits" constantly, and
    matching one of those as a heading would set the wrong direction for every
    transaction after it. Headings are short, few-worded, and — unlike prose —
    do not end in sentence punctuation.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 45:
        return False
    if stripped[-1] in ".,;:":
        return False
    if len(stripped.split()) > 5:
        return False
    return not _LEADING_DATE.match(stripped)


def _looks_encrypted(path: Path) -> bool:
    """Is there an /Encrypt entry in the file?

    The library's own failure for a locked PDF is an exception with an empty
    message, so the file itself is the more reliable witness.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(2048)
            fh.seek(max(0, path.stat().st_size - 4096))
            tail = fh.read()
    except OSError:
        return False
    return b"/Encrypt" in head or b"/Encrypt" in tail


def _open_pdf(path: Path, password: str | None):
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PdfExtractionError(
            "PDF support needs pdfplumber. Install it with: pip install pdfplumber"
        ) from exc
    try:
        return pdfplumber.open(str(path), password=password or "")
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        if _looks_encrypted(path) or "password" in detail.lower() or "encrypt" in detail.lower():
            hint = "the password in ledgerlens.yaml did not work" if password else (
                f"{path.name} is password protected"
            )
            raise PdfExtractionError(
                f"{hint}. Add 'pdf_password: yourpassword' to a ledgerlens.yaml "
                f"beside the file, or save an unlocked copy."
            ) from exc
        raise PdfExtractionError(f"{path.name} could not be opened as a PDF: {detail}") from exc


# --------------------------------------------------------------------------
# strategy 1: ruled tables
# --------------------------------------------------------------------------

def _from_tables(pdf) -> PdfExtraction | None:
    """Use a PDF's own ruling lines when it has them.

    Banks that draw a bordered grid hand the structure over for free, and
    reusing the CSV header matcher on the table's header row means the column
    synonyms — including the non-English ones — apply here too.
    """
    from ledgerlens.ingest.dialect import assign_columns

    collected: list[dict[str, str]] = []
    columns: dict[str, str] = {}
    header: list[str] = []

    for page in pdf.pages:
        for table in page.extract_tables():
            if len(table) < 2:
                continue
            for idx, candidate in enumerate(table[:3]):
                cells = [str(c or "").strip() for c in candidate]
                assigned = assign_columns(cells)
                has_money = ("debit" in assigned and "credit" in assigned) or "amount" in assigned
                if "date" in assigned and has_money:
                    if not columns:
                        columns, header = assigned, cells
                    for row in table[idx + 1 :]:
                        values = [str(c or "").strip() for c in row]
                        if not any(values):
                            continue
                        padded = values + [""] * (len(header) - len(values))
                        collected.append({header[i]: padded[i] for i in range(len(header))})
                    break

    if not columns or len(collected) < 2:
        return None

    return PdfExtraction(
        rows=collected,
        columns=columns,
        method="pdf_ruled_table",
        confidence=0.95,
        pages=len(pdf.pages),
        warnings=[],
    )


# --------------------------------------------------------------------------
# strategy 2: text layout
# --------------------------------------------------------------------------

def _collect_lines(pdf) -> tuple[list[dict[str, Any]], float]:
    """Flatten every page into ordered text lines, with the page width."""
    lines: list[dict[str, Any]] = []
    width = 612.0
    for page_no, page in enumerate(pdf.pages):
        width = max(width, float(page.width or width))
        try:
            page_lines = page.extract_text_lines()
        except Exception:  # pragma: no cover - malformed page
            continue
        for line in page_lines:
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            lines.append(
                {
                    "page": page_no,
                    "text": text,
                    "x0": float(line.get("x0", 0.0)),
                    "x1": float(line.get("x1", 0.0)),
                    "top": float(line.get("top", 0.0)),
                }
            )
    return lines, width


def _trailing_amount(text: str, money_threshold: float, line_x1: float) -> tuple[str, str] | None:
    """Split a row into (description, amount), or None if it has no amount.

    ``money_threshold`` is a page x-coordinate; only a token that ends near the
    right margin counts as this row's amount. Without that, a description
    containing a figure ("REFUND OF 12.00") would be read as the amount.
    """
    tokens = _money_tokens(text)
    if not tokens:
        return None
    # Approximate each token's x position by its share of the line's width.
    last_token, start, end = tokens[-1]
    if end < len(text.rstrip()) - 4:
        return None  # the rightmost money is not at the end of the row
    if line_x1 < money_threshold:
        return None
    description = text[:start].strip()
    return description, last_token


def _from_layout(pdf) -> PdfExtraction:
    lines, width = _collect_lines(pdf)
    if not lines:
        raise PdfExtractionError(
            "this PDF contains no extractable text, which usually means it is a "
            "scan or an image. Run it through OCR first (for example "
            "'ocrmypdf in.pdf out.pdf'), or export CSV from your bank instead."
        )

    money_threshold = width * _MONEY_COLUMN_FRACTION
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    sections_seen: list[str] = []

    section_sign: int | None = None
    section_name = "unknown"
    in_skip_section = False
    continuation_budget = 0
    expected_totals: dict[str, Decimal] = {}
    seen_totals: set[tuple[str, str]] = set()

    for line in lines:
        text = line["text"]

        # --- the statement's own section totals, for cross-checking ------
        # Collected regardless of section state: a section total lives *inside*
        # the summary block, which is the part we otherwise skip over.
        if _TOTAL_LINE.match(text) and not _YTD_TOTAL.search(text):
            money = _money_tokens(text)
            label = _MONEY.sub("", text).strip()
            label_sign, _, _ = _classify_heading(label)
            if money and label_sign is not None:
                value = _to_decimal(money[-1][0])
                if value is not None:
                    key = (re.sub(r"\s+", " ", label.lower()), str(value))
                    if key not in seen_totals:
                        seen_totals.add(key)
                        bucket = "in" if label_sign > 0 else "out"
                        expected_totals[bucket] = expected_totals.get(bucket, Decimal(0)) + value
            continue

        # --- section tracking -------------------------------------------
        sign, name, is_skip = _classify_heading(text)
        if _looks_like_heading(text):
            if _DETAIL_HEADING.search(text):
                # "Detail" / "Detail Continued" reopens the section whose
                # summary block we just walked past.
                in_skip_section = False
                continuation_budget = 0
                continue
            if is_skip:
                in_skip_section = True
                continuation_budget = 0
                continue
            if sign is not None:
                section_sign, section_name = sign, name or "unknown"
                in_skip_section = False
                continuation_budget = 0
                if section_name not in sections_seen:
                    sections_seen.append(section_name)
                continue

        date_match = _LEADING_DATE.match(text)

        # --- continuation of the previous transaction --------------------
        if not date_match:
            if rows and continuation_budget > 0 and not in_skip_section:
                from ledgerlens.enrich.foreign import parse_foreign_amount, parse_fx_rate

                # A foreign charge prints its original amount and conversion
                # rate on their own lines beneath it. Those carry money, so the
                # plain "no money here" test for a continuation line rejects
                # them — and they are the whole point of reading a travel
                # statement. They are kept apart from the description so that
                # "125.00 BRL" never ends up inside a merchant name.
                if parse_foreign_amount(text) or parse_fx_rate(text):
                    prior = rows[-1].get(COL_FOREIGN, "")
                    rows[-1][COL_FOREIGN] = f"{prior} {text}".strip()
                    continuation_budget -= 1
                elif not _is_metadata_line(text) and not _money_tokens(text):
                    rows[-1][COL_DESC] = f"{rows[-1][COL_DESC]} {text}".strip()
                    continuation_budget -= 1
            continue

        if in_skip_section or _NOT_A_TRANSACTION.match(text):
            continue

        split = _trailing_amount(text, money_threshold, line["x1"])
        if split is None:
            continue

        rest, amount = split
        raw_date = date_match.group(1)
        description = _LEADING_DATE.sub("", rest, count=1).strip()

        rows.append(
            {
                COL_DATE: raw_date,
                COL_DESC: description,
                COL_AMOUNT: amount,
                COL_SECTION: section_name,
                COL_FOREIGN: "",
                "_sign": str(section_sign) if section_sign is not None else "",
            }
        )
        continuation_budget = MAX_CONTINUATION_LINES

    if not rows:
        raise PdfExtractionError(
            "found text but no transaction rows. LedgerLens looks for rows that "
            "start with a date and end with an amount; this statement may use a "
            "layout it does not recognise yet. Exporting CSV from your bank is "
            "the reliable path."
        )

    kind, pre_signed, extra_warnings = _resolve_direction(rows, sections_seen)
    warnings.extend(extra_warnings)

    clean_rows = [{k: v for k, v in r.items() if k != "_sign"} for r in rows]
    reconciled, report, problems = _reconcile(clean_rows, expected_totals)
    warnings.extend(problems)

    if reconciled:
        confidence = 0.98
    elif reconciled is False:
        confidence = 0.45
    else:
        confidence = 0.9 if pre_signed else 0.7
        if not sections_seen:
            confidence = 0.6

    return PdfExtraction(
        rows=clean_rows,
        columns={"date": COL_DATE, "description": COL_DESC, "amount": COL_AMOUNT},
        method="pdf_text_layout",
        confidence=confidence,
        pages=len(pdf.pages),
        statement_kind=kind,
        pre_signed=pre_signed,
        warnings=warnings,
        sections_seen=sections_seen,
        reconciled=reconciled,
        reconciliation=report,
    )


def _resolve_direction(
    rows: list[dict[str, str]], sections_seen: list[str]
) -> tuple[str, bool, list[str]]:
    """Normalize every row's amount so that negative means money out.

    A credit card prints a purchase as positive and your payment as negative;
    a current account does the opposite. Reading one as the other inverts every
    figure in the database while looking entirely plausible, so the section
    headings — which state the direction outright — are preferred over any
    inference from the numbers.
    """
    card_markers = {"charges", "payments_credits", "payments", "credits", "fees", "interest"}
    bank_markers = {"deposits", "withdrawals"}
    seen = set(sections_seen)

    if seen & bank_markers:
        kind = "bank"
    elif seen & card_markers:
        kind = "credit_card"
    else:
        kind = "unknown"

    signed = [r for r in rows if r.get("_sign")]
    if not signed:
        return kind, False, [
            "no section headings recognised, so the direction of each amount is "
            "inferred from its sign alone"
        ]

    coverage = len(signed) / len(rows)
    warnings: list[str] = []
    if coverage < 0.95:
        warnings.append(
            f"{len(rows) - len(signed)} of {len(rows)} rows fell outside a recognised "
            f"section; their sign is taken from the statement as printed"
        )

    # Each section prints its ordinary case with a consistent sign — a card's
    # New Charges are positive, its Payments negative — and that printing is
    # simply the heading's direction restated, not an extra modifier on top of
    # it. Applying both inverts everything. So the majority sign within a
    # section is learned as that section's normal, and only the minority rows,
    # which are genuine reversals (a refund sitting among the charges), flip.
    by_section: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row.get("_sign"):
            by_section.setdefault(row[COL_SECTION], []).append(row)

    for section_rows in by_section.values():
        negatives = sum(1 for r in section_rows if _printed_negative(r[COL_AMOUNT]))
        majority_is_negative = negatives * 2 > len(section_rows)
        for row in section_rows:
            direction = int(row["_sign"])
            if _printed_negative(row[COL_AMOUNT]) != majority_is_negative:
                direction = -direction
            magnitude = row[COL_AMOUNT].lstrip("+-").strip().rstrip("-")
            row[COL_AMOUNT] = ("-" if direction < 0 else "") + magnitude

    return kind, True, warnings


def _printed_negative(raw: str) -> bool:
    text = raw.strip()
    return (
        text.startswith("-")
        or text.endswith("-")
        or text.upper().endswith("CR")
        or ("(" in text and ")" in text)
    )


def _to_decimal(raw: str) -> Decimal | None:
    """Parse a money token to a positive Decimal, or None if it isn't one."""
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _reconcile(
    rows: list[dict[str, str]], expected: dict[str, Decimal]
) -> tuple[bool | None, dict[str, str], list[str]]:
    """Check extracted rows against the statement's own printed section totals.

    This is the most valuable check in the PDF path. Reading a PDF is inherently
    a reconstruction, and its failures are silent ones: a dropped page, a row
    absorbed into the one above it, an inverted sign. The statement, helpfully,
    already states what its sections add up to. If the extracted rows sum to the
    same figures then the result is not merely plausible — it is arithmetically
    consistent with what the issuer printed.
    """
    if not expected:
        return None, {}, [
            "this statement prints no section totals, so the extraction could not "
            "be cross-checked against it"
        ]

    actual = {"in": Decimal(0), "out": Decimal(0)}
    for row in rows:
        value = _to_decimal(row[COL_AMOUNT])
        if value is None:
            continue
        bucket = "out" if row[COL_AMOUNT].strip().startswith("-") else "in"
        actual[bucket] += value

    report: dict[str, str] = {}
    problems: list[str] = []
    for bucket, want in expected.items():
        got = actual.get(bucket, Decimal(0))
        report[bucket] = f"statement {want}, extracted {got}"
        if abs(got - want) > _RECONCILE_TOLERANCE:
            label = "money in" if bucket == "in" else "money out"
            problems.append(
                f"{label} does not reconcile: the statement totals {want} but the rows "
                f"read from it come to {got} (off by {abs(got - want)}). Transactions "
                f"were probably missed or double-counted"
            )

    return (not problems), report, problems


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def extract(path: Path | str, *, password: str | None = None) -> PdfExtraction:
    """Recover transactions from the statement PDF at ``path``."""
    path = Path(path)
    pdf = _open_pdf(path, password)
    try:
        table_attempt = _from_tables(pdf)
        if table_attempt is not None:
            return table_attempt
        return _from_layout(pdf)
    finally:
        pdf.close()
