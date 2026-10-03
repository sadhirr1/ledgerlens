"""Import orchestration: CSV file in, normalized transactions out.

The loader is deliberately **idempotent**. People re-export overlapping date
ranges constantly — January-to-March in one download, February-to-April in the
next — and a naive importer doubles February. Two mechanisms prevent that:

* An import log keyed on the file's content hash short-circuits a byte-identical
  re-import entirely.
* Every transaction gets a deterministic content-hash id, so the same charge
  arriving from a *different* file still collapses to one row.

The occurrence counter in that hash is what keeps two genuinely identical
charges on the same day — two $4.50 coffees — from being mistaken for a
duplicate of each other.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ledgerlens.config import DialectOverride, load_override
from ledgerlens.db import get_or_create_account
from ledgerlens.enrich.categories import CategoryEngine
from ledgerlens.enrich.foreign import analyse as analyse_foreign
from ledgerlens.enrich.foreign import strip_fx_fragments
from ledgerlens.enrich.merchants import normalize_merchant
from ledgerlens.ingest.amounts import (
    AmountParseError,
    SignPlan,
    infer_sign_plan,
    parse_magnitude,
    signed_cents,
)
from ledgerlens.ingest.dates import DateParseError, resolve_date_format
from ledgerlens.ingest.dialect import Dialect, DialectError, detect_dialect, read_rows
from ledgerlens.ingest.pdf import PdfExtractionError
from ledgerlens.ingest.pdf import extract as pdf_extract

DATA_SUFFIXES = {".csv", ".tsv", ".txt", ".pdf"}
_WS = re.compile(r"\s+")


@dataclass
class ImportResult:
    """What happened during one file import."""

    path: str
    account: str
    rows_read: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dialect: dict[str, object] = field(default_factory=dict)
    already_imported: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "file": Path(self.path).name,
            "account": self.account,
            "rows_read": self.rows_read,
            "rows_inserted": self.rows_inserted,
            "rows_skipped": self.rows_skipped,
            "already_imported": self.already_imported,
            "warnings": self.warnings,
            "notes": self.notes,
            "errors": self.errors[:5],
            "error_count": len(self.errors),
        }


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


_PERIOD_TOKEN = re.compile(
    r"""^(?:
          \d{4}(?:q[1-4]|-?\d{1,2})?          # 2026, 2026q1, 2026-03
        | q[1-4](?:\d{4})?                    # q1, q12026
        | \d{1,2}[-_]\d{1,2}                  # 01-03
        | jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec
        | january|february|march|april|june|july|august
        | september|october|november|december
        | statements?|exports?|transactions?|download|activity|history
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _account_name_from_path(path: Path) -> str:
    """Derive an account name from a filename.

    Period markers are stripped so that two exports of the same account —
    ``chase_checking_2026q1.csv`` and ``chase_checking_2026q2.csv`` — land in
    one account and their overlapping transactions can deduplicate. Without
    this, every download would create a new account and February would appear
    twice.
    """
    tokens = [t for t in re.split(r"[\s_-]+", path.stem) if t]
    kept = [t for t in tokens if not _PERIOD_TOKEN.match(t)]

    # A bare "03" is a month only in the company of a year: "amex-gold-2026-03"
    # loses it, while an account genuinely named "Savings 2" keeps its number.
    if len(kept) != len(tokens):
        kept = [t for t in kept if not re.fullmatch(r"\d{1,2}", t)]

    name = _WS.sub(" ", " ".join(kept)).strip()
    return name.title() or path.stem


def _norm_desc(raw: str) -> str:
    return _WS.sub(" ", str(raw or "").strip().lower())


def _txn_id(account: str, iso_date: str, cents: int, desc: str, occurrence: int) -> str:
    payload = f"{account}|{iso_date}|{cents}|{_norm_desc(desc)}|{occurrence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def import_file(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    override: DialectOverride | None = None,
    engine: CategoryEngine | None = None,
    force: bool = False,
) -> ImportResult:
    """Import one statement file into ``conn``."""
    path = Path(path)
    override = override or DialectOverride()
    engine = engine or CategoryEngine.load()
    account = override.account_name or _account_name_from_path(path)
    result = ImportResult(path=str(path), account=account)

    digest = _file_hash(path)
    if not force:
        seen = conn.execute(
            "SELECT rows_inserted FROM import_log WHERE file_hash = ?", (digest,)
        ).fetchone()
        if seen is not None:
            result.already_imported = True
            result.warnings.append("identical file already imported; skipped")
            return result

    try:
        rows, dialect, forced_sign_plan = _read_source(path, override, result)
    except (DialectError, PdfExtractionError) as exc:
        result.errors.append(str(exc))
        return result

    result.rows_read = len(rows)
    if not rows:
        result.warnings.append("file has a header but no data rows")
        return result

    cols = dialect.columns
    date_col = cols.get("date")
    if not date_col:
        result.errors.append("no date column found")
        return result

    # --- resolve file-level plans before touching individual rows -----------
    date_samples = [str(r.get(date_col, "")) for r in rows]
    try:
        date_plan = resolve_date_format(
            date_samples,
            day_first_hint=override.day_first,
            explicit_format=override.date_format,
        )
    except DateParseError as exc:
        result.errors.append(f"date column {date_col!r}: {exc}")
        return result
    if date_plan.confidence < 0.9:
        result.warnings.append(f"date format: {date_plan.describe()}")

    amount_col = cols.get("amount")
    type_col = cols.get("type")
    signed_samples = []
    if amount_col:
        for r in rows:
            try:
                signed_samples.append(parse_magnitude(str(r.get(amount_col, ""))))
            except AmountParseError:
                continue

    # A PDF extractor works out direction from the statement's own section
    # headings, which state it outright; that beats inferring it from the
    # numbers, so its conclusion is taken as given.
    sign_plan = forced_sign_plan or infer_sign_plan(
        has_debit_credit=dialect.has_debit_credit,
        signed_samples=signed_samples,
        type_samples=[str(r.get(type_col, "")) for r in rows] if type_col else None,
        override=override.negative_is_outflow,
    )
    if sign_plan.confidence < 0.9:
        result.warnings.append(f"amount direction: {sign_plan.describe()}")

    result.dialect["date_plan"] = date_plan.describe()
    result.dialect["sign_plan"] = sign_plan.describe()

    # --- row loop -----------------------------------------------------------
    currency = override.currency or "USD"
    account_id = get_or_create_account(conn, account, currency=currency)
    desc_col = cols.get("description")
    seen_keys: dict[tuple[str, int, str], int] = {}
    inserted = 0

    for offset, row in enumerate(rows, start=dialect.header_row + 2):
        raw_date = str(row.get(date_col, "")).strip()
        if not raw_date:
            result.rows_skipped += 1
            continue
        try:
            posted = date_plan.parse(raw_date)
        except ValueError:
            result.rows_skipped += 1
            result.errors.append(f"row {offset}: unparseable date {raw_date!r}")
            continue

        try:
            cents = signed_cents(
                sign_plan,
                amount_raw=str(row.get(amount_col, "")) if amount_col else None,
                debit_raw=str(row.get(cols["debit"], "")) if "debit" in cols else None,
                credit_raw=str(row.get(cols["credit"], "")) if "credit" in cols else None,
                type_raw=str(row.get(type_col, "")) if type_col else None,
            )
        except AmountParseError as exc:
            result.rows_skipped += 1
            result.errors.append(f"row {offset}: {exc}")
            continue

        if cents == 0:
            result.rows_skipped += 1
            continue

        raw_desc = str(row.get(desc_col, "")).strip() if desc_col else ""

        # A PDF prints the original amount and conversion rate on separate lines
        # beneath the charge; the extractor keeps those in their own field so
        # they stay out of the merchant name. A CSV usually folds them into the
        # descriptor, so both are offered to the analyser.
        foreign = analyse_foreign(
            raw_desc,
            extra_text=str(row.get("ForeignDetail", "")),
            billed_cents=cents,
            home_currency=currency,
        )
        merchant = normalize_merchant(strip_fx_fragments(raw_desc))
        category = engine.categorize(merchant, raw_desc)
        row_currency = str(row.get(cols.get("currency", ""), "")).strip() or currency

        key = (posted.isoformat(), cents, _norm_desc(raw_desc))
        occurrence = seen_keys.get(key, 0)
        seen_keys[key] = occurrence + 1

        txn_id = _txn_id(account, posted.isoformat(), cents, raw_desc, occurrence)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transactions
                (id, account_id, posted_on, amount_cents, currency, raw_description,
                 merchant, category, source_file, source_row, occurrence,
                 original_amount_cents, original_currency, fx_rate, country, is_fee)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id, account_id, posted.isoformat(), cents, row_currency,
                raw_desc, merchant, category, path.name, offset, occurrence,
                foreign.original_amount_cents, foreign.original_currency,
                foreign.fx_rate, foreign.country, int(foreign.is_fee),
            ),
        )
        if cur.rowcount:
            inserted += 1
        else:
            result.rows_skipped += 1

    result.rows_inserted = inserted
    conn.execute(
        """
        INSERT OR REPLACE INTO import_log
            (file_hash, path, imported_at, rows_read, rows_inserted, rows_skipped, dialect_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            digest, str(path), datetime.now(timezone.utc).isoformat(timespec="seconds"),
            result.rows_read, result.rows_inserted, result.rows_skipped,
            json.dumps(result.dialect, default=str),
        ),
    )
    conn.commit()
    return result


def recategorize(conn: sqlite3.Connection, engine: CategoryEngine | None = None) -> dict[str, int]:
    """Re-run categorization over already-imported transactions.

    New rules should apply to history without a re-import, and re-importing
    would not achieve it anyway: transaction ids are content hashes, so an
    existing row is left untouched rather than overwritten.
    """
    engine = engine or CategoryEngine.load()
    rows = conn.execute("SELECT id, merchant, raw_description, category FROM transactions").fetchall()
    changed = 0
    for row in rows:
        new_category = engine.categorize(row["merchant"], row["raw_description"])
        if new_category != row["category"]:
            conn.execute(
                "UPDATE transactions SET category = ? WHERE id = ?", (new_category, row["id"])
            )
            changed += 1
    conn.commit()
    return {"examined": len(rows), "changed": changed}


def _read_source(
    path: Path, override: DialectOverride, result: ImportResult
) -> tuple[list[dict[str, str]], Dialect, SignPlan | None]:
    """Read a statement file into rows, whatever format it arrived in.

    Both formats converge on the same shape — a list of row dicts plus a mapping
    of roles to column names — so everything downstream (date resolution,
    merchant normalization, categorization, dedup) is shared rather than written
    twice and drifting apart.
    """
    if path.suffix.lower() == ".pdf":
        extraction = pdf_extract(path, password=override.pdf_password)
        result.warnings.extend(extraction.warnings)
        result.dialect = extraction.to_dict()

        dialect = Dialect(
            encoding="pdf",
            delimiter="",
            header_row=0,
            columns=dict(extraction.columns),
            header=list(extraction.columns.values()),
        )
        _apply_override(dialect, override)

        forced = None
        if extraction.pre_signed:
            forced = SignPlan(
                mode="signed",
                negative_is_outflow=True,
                confidence=1.0,
                reason=(
                    "direction taken from the statement's own section headings "
                    f"({', '.join(extraction.sections_seen) or 'none found'})"
                ),
            )
        if extraction.reconciled:
            detail = "; ".join(f"{k} {v}" for k, v in extraction.reconciliation.items())
            result.notes.append(
                f"reconciles against the statement's own printed totals ({detail})"
            )
        return extraction.rows, dialect, forced

    dialect = detect_dialect(path, skip_rows=override.skip_rows)
    _apply_override(dialect, override)
    result.dialect = dialect.to_dict()
    return read_rows(path, dialect), dialect, None


def _apply_override(dialect: Dialect, override: DialectOverride) -> None:
    """Let explicit user column choices win over auto-detection."""
    mapping = {
        "date": override.date_column,
        "amount": override.amount_column,
        "debit": override.debit_column,
        "credit": override.credit_column,
        "description": override.description_column,
    }
    for role, column in mapping.items():
        if column:
            dialect.columns[role] = column


def import_folder(
    conn: sqlite3.Connection,
    folder: Path | str,
    *,
    engine: CategoryEngine | None = None,
    force: bool = False,
) -> list[ImportResult]:
    """Import every statement file in ``folder`` (non-recursive)."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not a directory")

    override = load_override(folder)
    engine = engine or CategoryEngine.load()
    results: list[ImportResult] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in DATA_SUFFIXES:
            continue
        if path.name == "ledgerlens.yaml":
            continue
        results.append(
            import_file(conn, path, override=override, engine=engine, force=force)
        )
    return results
