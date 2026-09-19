"""Detecting what shape a bank's CSV export actually is.

Every institution exports something slightly different: a preamble of account
blurb above the real header, semicolon delimiters, ``windows-1252`` smart
quotes, a running-balance column sitting next to the amount, debit and credit
split across two columns. This module works out the layout so the rest of the
pipeline can assume a single normal form.

Column roles are resolved by scoring each header cell against ordered synonym
lists and then assigning greedily, best score first, so that a file with both
``Transaction Date`` and ``Posted Date`` picks the transaction date rather than
whichever happened to come first.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
DELIMITERS = (",", ";", "\t", "|")
MAX_PREAMBLE_ROWS = 25

# Ordered best-first. Earlier entries win ties.
ROLE_SYNONYMS: dict[str, list[str]] = {
    # "Date" outranks "Posted Date": when a file has both, the first is the
    # transaction date and the second is settlement, and the former is what
    # people mean when they ask when something happened.
    "date": [
        "transaction date", "trans date", "trans. date", "booking date",
        "value date", "completed date", "date", "posting date", "date posted",
        "posted date", "post date",
        # de / fr / es
        "buchungstag", "wertstellung", "datum",
        "date d'operation", "date d'opération", "date operation",
        "fecha operacion", "fecha", "data",
    ],
    "type": [
        "dr/cr", "cr/dr", "debit/credit", "transaction type", "type",
        "indicator", "direction",
    ],
    "debit": [
        "debit amount", "withdrawal amount", "money out", "paid out",
        "withdrawals", "withdrawal", "debit", "outflow",
    ],
    "credit": [
        "credit amount", "deposit amount", "money in", "paid in",
        "deposits", "deposit", "credit", "inflow",
    ],
    "amount": [
        "transaction amount", "amount", "amt", "net amount", "gross amount",
        "value",
        # de / fr / es
        "betrag", "umsatz", "montant", "importe", "valor",
    ],
    "description": [
        "original description", "transaction description", "description",
        "narrative", "particulars", "payee", "merchant", "details", "memo",
        "reference", "name",
        # de / fr / es
        "verwendungszweck", "buchungstext", "beguenstigter", "begünstigter",
        "libelle", "libellé", "concepto", "descripcion", "descripción",
    ],
    "currency": ["currency", "ccy", "curr", "waehrung", "währung", "devise", "moneda"],
}

# A running balance column is numeric and often sits beside the amount; never
# let it be mistaken for one.
_NEVER_AMOUNT = ("balance", "running total", "ledger bal")


class DialectError(ValueError):
    """Raised when a file cannot be recognised as a statement export."""


@dataclass
class Dialect:
    """The resolved physical layout of one CSV file."""

    encoding: str
    delimiter: str
    header_row: int
    columns: dict[str, str] = field(default_factory=dict)
    header: list[str] = field(default_factory=list)

    @property
    def has_debit_credit(self) -> bool:
        return "debit" in self.columns and "credit" in self.columns

    def to_dict(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "header_row": self.header_row,
            "columns": dict(self.columns),
        }


def detect_encoding(path: Path) -> str:
    """Return the first encoding in :data:`ENCODINGS` that decodes the file."""
    raw = Path(path).read_bytes()
    for enc in ENCODINGS:
        try:
            raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    return "latin-1"  # pragma: no cover - latin-1 decodes any byte string


def detect_delimiter(sample: str) -> str:
    """Guess the field delimiter from a text sample."""
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        pass
    lines = [ln for ln in sample.splitlines() if ln.strip()][:MAX_PREAMBLE_ROWS]
    best, best_count = ",", 0
    for delim in DELIMITERS:
        counts = [ln.count(delim) for ln in lines]
        # A real delimiter appears consistently, more than once per line.
        consistent = [c for c in counts if c > 0]
        if len(consistent) >= max(1, len(lines) // 2):
            score = min(consistent) * len(consistent)
            if score > best_count:
                best, best_count = delim, score
    return best


def _norm(cell: str) -> str:
    return " ".join(str(cell or "").strip().lower().replace("_", " ").split())


def _score(cell: str, synonyms: list[str]) -> int:
    """Score one header cell against a role's synonym list. 0 means no match."""
    text = _norm(cell)
    if not text:
        return 0
    for rank, syn in enumerate(synonyms):
        weight = len(synonyms) - rank
        if text == syn:
            return 1000 + weight
        if text.startswith(syn) or text.endswith(syn):
            return 500 + weight
        if syn in text:
            return 200 + weight
    return 0


def assign_columns(header: list[str]) -> dict[str, str]:
    """Map role -> header cell for a candidate header row."""
    candidates: list[tuple[int, str, str]] = []
    for role, synonyms in ROLE_SYNONYMS.items():
        for cell in header:
            if not str(cell or "").strip():
                continue
            if role in ("amount", "debit", "credit") and any(
                bad in _norm(cell) for bad in _NEVER_AMOUNT
            ):
                continue
            score = _score(cell, synonyms)
            if score:
                candidates.append((score, role, cell))

    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for _, role, cell in candidates:
        if role in assigned or cell in used:
            continue
        assigned[role] = cell
        used.add(cell)

    # A lone debit or credit column with no counterpart is really an amount.
    if ("debit" in assigned) ^ ("credit" in assigned):
        orphan = "debit" if "debit" in assigned else "credit"
        if "amount" not in assigned:
            assigned["amount"] = assigned.pop(orphan)
        else:
            assigned.pop(orphan)

    return assigned


def _header_score(assigned: dict[str, str]) -> int:
    """How complete a candidate header is. Needs date + money + description."""
    has_money = ("debit" in assigned and "credit" in assigned) or "amount" in assigned
    return (
        3 * ("date" in assigned)
        + 3 * has_money
        + 2 * ("description" in assigned)
        + 1 * ("type" in assigned)
    )


def detect_dialect(path: Path, *, encoding: str | None = None, skip_rows: int = 0) -> Dialect:
    """Work out encoding, delimiter, header row and column roles for ``path``."""
    path = Path(path)
    enc = encoding or detect_encoding(path)
    text = path.read_text(encoding=enc)
    if not text.strip():
        raise DialectError(f"{path.name} is empty")

    delimiter = detect_delimiter("\n".join(text.splitlines()[:MAX_PREAMBLE_ROWS]))
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise DialectError(f"{path.name} contains no rows")

    best: tuple[int, int, dict[str, str], list[str]] | None = None
    limit = min(len(rows), skip_rows + MAX_PREAMBLE_ROWS)
    for idx in range(skip_rows, limit):
        row = rows[idx]
        if not any(str(c or "").strip() for c in row):
            continue
        assigned = assign_columns(row)
        score = _header_score(assigned)
        if best is None or score > best[1]:
            best = (idx, score, assigned, row)
        if score >= 9:  # date + money + description + type: cannot do better
            break

    if best is None or best[1] < 6:
        found = best[3] if best else []
        raise DialectError(
            f"{path.name}: could not find a header row with a date, an amount and a "
            f"description. Best candidate was {found!r}. Add a ledgerlens.yaml "
            f"override to map the columns explicitly."
        )

    header_row, _, assigned, header = best
    return Dialect(
        encoding=enc,
        delimiter=delimiter,
        header_row=header_row,
        columns=assigned,
        header=[str(h) for h in header],
    )


def read_rows(path: Path, dialect: Dialect) -> list[dict[str, str]]:
    """Read data rows below the header as dicts keyed by header cell."""
    text = Path(path).read_text(encoding=dialect.encoding)
    rows = list(csv.reader(io.StringIO(text), delimiter=dialect.delimiter))
    header = [str(h) for h in rows[dialect.header_row]]
    out: list[dict[str, str]] = []
    for row in rows[dialect.header_row + 1 :]:
        if not any(str(c or "").strip() for c in row):
            continue
        padded = list(row) + [""] * (len(header) - len(row))
        out.append({header[i]: padded[i] for i in range(len(header))})
    return out
