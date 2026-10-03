"""Aggregation queries, written for a context window rather than a screen.

Tool results are consumed by a language model, and the budget is tokens rather
than pixels. Two rules follow from that and are enforced here rather than left
to each tool:

* **Aggregate by default, detail on request.** Asking "what did I spend on food"
  should return a dozen category totals, not four thousand line items.
* **Truncate loudly.** Every capped result carries ``truncated``, the number of
  rows returned and the true total, so the model can say "showing 50 of 812"
  instead of confidently summarising a slice it thinks is the whole set.

Amounts cross this boundary as float dollars rounded to cents — integer cents
are correct internally but read badly to a model, which then reports "-450" as
a dollar figure. Sign is preserved: negative is money out.
"""

from __future__ import annotations

import sqlite3
from typing import Any

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _money(cents: int | None) -> float:
    return round((cents or 0) / 100, 2)


def _clamp(limit: int | None) -> int:
    if not limit or limit < 1:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


def _window(start: str | None, end: str | None) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if start:
        clauses.append("posted_on >= ?")
        params.append(start)
    if end:
        clauses.append("posted_on <= ?")
        params.append(end)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """High-level shape of the database — the natural first tool call."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               MIN(posted_on) AS lo,
               MAX(posted_on) AS hi,
               SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END) AS out_cents,
               SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS in_cents
        FROM transactions
        """
    ).fetchone()
    accounts = conn.execute(
        """
        SELECT a.name, COUNT(t.id) AS n, MIN(t.posted_on) AS lo, MAX(t.posted_on) AS hi
        FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id
        GROUP BY a.id ORDER BY n DESC
        """
    ).fetchall()
    uncategorized = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = 'Uncategorized'"
    ).fetchone()["n"]

    return {
        "transaction_count": row["n"],
        "date_range": {"start": row["lo"], "end": row["hi"]},
        "total_out": _money(row["out_cents"]),
        "total_in": _money(row["in_cents"]),
        "net": _money((row["in_cents"] or 0) - (row["out_cents"] or 0)),
        "uncategorized_count": uncategorized,
        "accounts": [
            {"name": a["name"], "transactions": a["n"], "from": a["lo"], "to": a["hi"]}
            for a in accounts
        ],
    }


def spending_by_category(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    where, params = _window(start, end)
    limit = _clamp(limit)
    rows = conn.execute(
        f"""
        SELECT category,
               SUM(-amount_cents) AS spent_cents,
               COUNT(*) AS n
        FROM transactions
        WHERE {where} AND amount_cents < 0
        GROUP BY category
        ORDER BY spent_cents DESC
        LIMIT ?
        """,
        [*params, limit + 1],
    ).fetchall()

    truncated = len(rows) > limit
    rows = rows[:limit]
    total = conn.execute(
        f"SELECT SUM(-amount_cents) AS s FROM transactions WHERE {where} AND amount_cents < 0",
        params,
    ).fetchone()["s"]

    return {
        "period": {"start": start, "end": end},
        "total_spent": _money(total),
        "categories": [
            {
                "category": r["category"],
                "spent": _money(r["spent_cents"]),
                "transactions": r["n"],
                "share": round((r["spent_cents"] / total) * 100, 1) if total else 0.0,
            }
            for r in rows
        ],
        "truncated": truncated,
    }


def spending_by_merchant(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    where, params = _window(start, end)
    if category:
        where += " AND category = ?"
        params.append(category)
    limit = _clamp(limit)

    rows = conn.execute(
        f"""
        SELECT merchant, category,
               SUM(-amount_cents) AS spent_cents,
               COUNT(*) AS n,
               MIN(posted_on) AS lo, MAX(posted_on) AS hi
        FROM transactions
        WHERE {where} AND amount_cents < 0
        GROUP BY merchant
        ORDER BY spent_cents DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    distinct = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM (
            SELECT merchant FROM transactions
            WHERE {where} AND amount_cents < 0 GROUP BY merchant
        )
        """,
        params,
    ).fetchone()["n"]

    return {
        "period": {"start": start, "end": end},
        "category": category,
        "merchants": [
            {
                "merchant": r["merchant"],
                "category": r["category"],
                "spent": _money(r["spent_cents"]),
                "transactions": r["n"],
                "first": r["lo"],
                "last": r["hi"],
            }
            for r in rows
        ],
        "returned": len(rows),
        "total_merchants": distinct,
        "truncated": distinct > len(rows),
    }


def monthly_summary(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    where, params = _window(start, end)
    limit = _clamp(limit)
    rows = conn.execute(
        f"""
        SELECT substr(posted_on, 1, 7) AS month,
               SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END) AS out_cents,
               SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS in_cents,
               COUNT(*) AS n
        FROM transactions
        WHERE {where}
        GROUP BY month
        ORDER BY month DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    months = [
        {
            "month": r["month"],
            "spent": _money(r["out_cents"]),
            "received": _money(r["in_cents"]),
            "net": _money(r["in_cents"] - r["out_cents"]),
            "transactions": r["n"],
        }
        for r in rows
    ]
    spends = [m["spent"] for m in months]
    return {
        "months": months,
        "average_monthly_spend": round(sum(spends) / len(spends), 2) if spends else 0.0,
        "truncated": len(rows) == limit,
    }


def list_transactions(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Line-item drill-down. Deliberately capped and always reports the cap."""
    where, params = _window(start, end)
    if merchant:
        where += " AND merchant LIKE ?"
        params.append(f"%{merchant}%")
    if category:
        where += " AND category = ?"
        params.append(category)
    if min_amount is not None:
        where += " AND ABS(amount_cents) >= ?"
        params.append(int(round(min_amount * 100)))
    if max_amount is not None:
        where += " AND ABS(amount_cents) <= ?"
        params.append(int(round(max_amount * 100)))

    limit = _clamp(limit)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM transactions WHERE {where}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT posted_on, merchant, category, amount_cents, raw_description
        FROM transactions WHERE {where}
        ORDER BY posted_on DESC, ABS(amount_cents) DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    return {
        "transactions": [
            {
                "date": r["posted_on"],
                "merchant": r["merchant"],
                "category": r["category"],
                "amount": _money(r["amount_cents"]),
                "description": r["raw_description"][:80],
            }
            for r in rows
        ],
        "returned": len(rows),
        "total_matching": total,
        "truncated": total > len(rows),
    }


def top_uncategorized(conn: sqlite3.Connection, *, limit: int | None = None) -> dict[str, Any]:
    """Merchants with no category, ranked by spend — the rule-writing worklist."""
    limit = _clamp(limit)
    rows = conn.execute(
        """
        SELECT merchant,
               SUM(-amount_cents) AS spent_cents,
               COUNT(*) AS n,
               MAX(raw_description) AS sample
        FROM transactions
        WHERE category = 'Uncategorized' AND amount_cents < 0
        GROUP BY merchant
        ORDER BY spent_cents DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return {
        "merchants": [
            {
                "merchant": r["merchant"],
                "spent": _money(r["spent_cents"]),
                "transactions": r["n"],
                "example_description": r["sample"][:80],
            }
            for r in rows
        ],
        "returned": len(rows),
    }


def compare_periods(
    conn: sqlite3.Connection,
    *,
    period_a: tuple[str, str],
    period_b: tuple[str, str],
    limit: int | None = None,
) -> dict[str, Any]:
    """Category-level diff between two windows, biggest movers first."""
    limit = _clamp(limit)

    def totals(window: tuple[str, str]) -> dict[str, int]:
        rows = conn.execute(
            """
            SELECT category, SUM(-amount_cents) AS c FROM transactions
            WHERE posted_on >= ? AND posted_on <= ? AND amount_cents < 0
            GROUP BY category
            """,
            window,
        ).fetchall()
        return {r["category"]: r["c"] for r in rows}

    a, b = totals(period_a), totals(period_b)
    changes = []
    for category in set(a) | set(b):
        av, bv = a.get(category, 0), b.get(category, 0)
        changes.append(
            {
                "category": category,
                "period_a": _money(av),
                "period_b": _money(bv),
                "change": _money(bv - av),
                "percent_change": round(((bv - av) / av) * 100, 1) if av else None,
            }
        )
    changes.sort(key=lambda c: -abs(c["change"]))

    return {
        "period_a": {"start": period_a[0], "end": period_a[1], "total": _money(sum(a.values()))},
        "period_b": {"start": period_b[0], "end": period_b[1], "total": _money(sum(b.values()))},
        "changes": changes[:limit],
        "truncated": len(changes) > limit,
    }


def all_transactions_for_recurrence(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Full outflow set, for in-process periodicity analysis (never returned raw)."""
    rows = conn.execute(
        """
        SELECT posted_on, merchant, category, amount_cents
        FROM transactions WHERE amount_cents < 0
        ORDER BY posted_on
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _country_label(code: str) -> str:
    """Name a country bucket.

    A foreign transaction fee has no country of its own, and bundling it into
    the home-country total reads as domestic spending when it is the opposite —
    a cost of having been abroad. It gets its own line.
    """
    from ledgerlens.enrich.foreign import country_name

    if code == "__fee__":
        return "Foreign transaction fees"
    return country_name(code) if code else "Home country"


def spending_by_country(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Spending grouped by the country a charge happened in.

    Domestic transactions have no country and are reported as one bucket rather
    than silently dropped, so the shares still add up to the whole.
    """
    where, params = _window(start, end)
    limit = _clamp(limit)
    rows = conn.execute(
        f"""
        SELECT CASE WHEN is_fee THEN '__fee__' ELSE COALESCE(country, '') END AS code,
               SUM(-amount_cents) AS spent_cents,
               COUNT(*) AS n,
               MIN(posted_on) AS lo, MAX(posted_on) AS hi
        FROM transactions
        WHERE {where} AND amount_cents < 0
        GROUP BY code
        ORDER BY spent_cents DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()

    total = conn.execute(
        f"SELECT SUM(-amount_cents) AS s FROM transactions WHERE {where} AND amount_cents < 0",
        params,
    ).fetchone()["s"] or 0

    return {
        "period": {"start": start, "end": end},
        "total_spent": _money(total),
        "countries": [
            {
                "country": _country_label(r["code"]),
                "code": r["code"] if r["code"] and r["code"] != "__fee__" else None,
                "spent": _money(r["spent_cents"]),
                "transactions": r["n"],
                "first": r["lo"],
                "last": r["hi"],
                "share": round(r["spent_cents"] / total * 100, 1) if total else 0.0,
            }
            for r in rows
        ],
        "truncated": len(rows) == limit,
    }


def travel_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rows needed for trip and conversion analysis, done in process."""
    rows = conn.execute(
        """
        SELECT posted_on, merchant, category, amount_cents, country,
               original_amount_cents, original_currency, fx_rate, is_fee
        FROM transactions
        ORDER BY posted_on
        """
    ).fetchall()
    return [dict(r) for r in rows]
