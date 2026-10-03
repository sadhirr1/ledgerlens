"""SQLite storage layer.

Design notes
------------
* **Money is stored as signed integer cents.** Floats are never used for
  currency anywhere in this codebase. ``-1250`` means $12.50 left the account.
* **Sign convention is normalized on the way in**: negative = money out,
  positive = money in. Whatever convention a bank used in its export is
  resolved during ingestion (see :mod:`ledgerlens.ingest.amounts`) so that
  every row in this table means the same thing.
* **Transaction IDs are content hashes**, which makes re-importing an
  overlapping date range a no-op instead of a duplicate.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    institution TEXT,
    mask        TEXT,
    currency    TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    posted_on       TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    raw_description TEXT NOT NULL,
    merchant        TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Uncategorized',
    source_file     TEXT NOT NULL,
    source_row      INTEGER NOT NULL,
    occurrence      INTEGER NOT NULL DEFAULT 0,
    -- Set only when a charge happened abroad. original_amount_cents is what the
    -- merchant actually charged in local currency; amount_cents is what the
    -- issuer billed after converting it.
    original_amount_cents INTEGER,
    original_currency     TEXT,
    fx_rate               REAL,
    country               TEXT,
    is_fee                INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_tx_posted   ON transactions(posted_on);
CREATE INDEX IF NOT EXISTS ix_tx_merchant ON transactions(merchant);
CREATE INDEX IF NOT EXISTS ix_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS ix_tx_account  ON transactions(account_id);
CREATE INDEX IF NOT EXISTS ix_tx_country  ON transactions(country);

CREATE TABLE IF NOT EXISTS import_log (
    file_hash     TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    rows_read     INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_skipped  INTEGER NOT NULL,
    dialect_json  TEXT NOT NULL
);
"""


# Columns added after v1. A database built by an earlier version is upgraded
# in place rather than rebuilt, so nobody loses an import history to an upgrade.
_ADDED_SINCE_V1 = {
    "original_amount_cents": "INTEGER",
    "original_currency": "TEXT",
    "fx_rate": "REAL",
    "country": "TEXT",
    "is_fee": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    for column, decl in _ADDED_SINCE_V1.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {decl}")
    conn.commit()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the LedgerLens database at ``db_path``."""
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a connection, committing on clean exit."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_account(
    conn: sqlite3.Connection,
    name: str,
    institution: str | None = None,
    mask: str | None = None,
    currency: str = "USD",
) -> int:
    """Return the id of the account called ``name``, creating it if absent."""
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts(name, institution, mask, currency) VALUES (?, ?, ?, ?)",
        (name, institution, mask, currency),
    )
    return int(cur.lastrowid)


def transaction_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])


def date_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT MIN(posted_on) AS lo, MAX(posted_on) AS hi FROM transactions"
    ).fetchone()
    return (row["lo"], row["hi"])
