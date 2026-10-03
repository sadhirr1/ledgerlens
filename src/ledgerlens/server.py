"""The MCP tool surface.

Tool design here follows one principle: the model should never have to ask for
less. Defaults are aggregates, every list is capped, and every capped result
says so. ``get_transactions`` is the only line-item tool and it still refuses to
return more than 200 rows, because a tool that can flood a context window
eventually will.

Every tool is read-only except :func:`import_statements`, and even that only
writes to LedgerLens's own database — source CSVs are never modified.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x, where the same class was called FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

from ledgerlens import query
from ledgerlens.config import db_path, user_rules_path
from ledgerlens.db import session
from ledgerlens.enrich.categories import CategoryEngine
from ledgerlens.enrich.recurring import detect_subscriptions
from ledgerlens.enrich.travel import detect_trips, foreign_cost_summary, unresolved_locations
from ledgerlens.ingest import import_file, import_folder, recategorize

# The MCP SDK renamed FastMCP to MCPServer in 2.0. The decorator, the tool
# registry and run() are otherwise unchanged, so one alias covers both and the
# server keeps working whichever generation of the SDK a user has installed.
mcp = _Server("ledgerlens")


def _engine() -> CategoryEngine:
    return CategoryEngine.load(user_rules_path())


@mcp.tool()
def import_statements(path: str, force: bool = False) -> dict[str, Any]:
    """Import bank or card statement CSVs into the local database.

    Point this at a folder of exports (or a single file). Formats, date
    conventions and debit/credit direction are detected automatically. Re-running
    it is safe: already-imported files are skipped and duplicate transactions
    collapse, so overlapping date ranges will not double-count.

    Args:
        path: Folder containing statement CSVs, or a path to one CSV file.
        force: Re-import files that were already imported.

    Returns:
        Per-file import counts, plus any low-confidence detection warnings that
        are worth showing the user.
    """
    target = Path(path).expanduser()
    if not target.exists():
        return {"error": f"{target} does not exist"}

    engine = _engine()
    with session(db_path()) as conn:
        if target.is_dir():
            results = import_folder(conn, target, engine=engine, force=force)
        else:
            results = [import_file(conn, target, engine=engine, force=force)]
        summary = query.overview(conn)

    return {
        "imported": [r.to_dict() for r in results],
        "total_inserted": sum(r.rows_inserted for r in results),
        "database": summary,
    }


@mcp.tool()
def overview() -> dict[str, Any]:
    """Summarise what statement data is loaded: date range, totals, accounts.

    Call this first when you do not know what the user has imported.
    """
    with session(db_path()) as conn:
        return query.overview(conn)


@mcp.tool()
def spending_by_category(
    start: str | None = None, end: str | None = None, limit: int = 25
) -> dict[str, Any]:
    """Total spending grouped by category, largest first.

    Args:
        start: Inclusive ISO date (YYYY-MM-DD). Omit for all history.
        end: Inclusive ISO date (YYYY-MM-DD). Omit for all history.
        limit: Maximum categories to return (capped at 200).
    """
    with session(db_path()) as conn:
        return query.spending_by_category(conn, start=start, end=end, limit=limit)


@mcp.tool()
def spending_by_merchant(
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Total spending grouped by merchant, largest first.

    Args:
        start: Inclusive ISO date (YYYY-MM-DD).
        end: Inclusive ISO date (YYYY-MM-DD).
        category: Restrict to one category, e.g. "Dining".
        limit: Maximum merchants to return (capped at 200).
    """
    with session(db_path()) as conn:
        return query.spending_by_merchant(
            conn, start=start, end=end, category=category, limit=limit
        )


@mcp.tool()
def monthly_summary(
    start: str | None = None, end: str | None = None, limit: int = 24
) -> dict[str, Any]:
    """Money in, money out and net position for each month, most recent first.

    Args:
        start: Inclusive ISO date (YYYY-MM-DD).
        end: Inclusive ISO date (YYYY-MM-DD).
        limit: Maximum months to return.
    """
    with session(db_path()) as conn:
        return query.monthly_summary(conn, start=start, end=end, limit=limit)


@mcp.tool()
def find_subscriptions(
    status: str | None = None,
    min_confidence: float = 0.55,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Find recurring charges — subscriptions, memberships, regular bills.

    Detects periodicity per merchant and reports what each one costs per year.
    Charges whose next payment is well overdue are marked ``likely_cancelled``,
    which separates "you are still paying for this" from "this already stopped".

    Args:
        status: Filter to "active", "overdue" or "likely_cancelled".
        min_confidence: Detection threshold from 0 to 1. Lower finds more and
            is noisier.
        as_of: Judge active/cancelled as at this ISO date instead of today.

    Returns:
        Detected subscriptions sorted by annual cost, with a total for the
        active ones.
    """
    reference = date.fromisoformat(as_of) if as_of else None
    with session(db_path()) as conn:
        rows = query.all_transactions_for_recurrence(conn)

    subs = detect_subscriptions(rows, as_of=reference, min_confidence=min_confidence)
    if status:
        subs = [s for s in subs if s.status == status]

    active = [s for s in subs if s.status == "active"]
    return {
        "subscriptions": [s.to_dict() for s in subs],
        "count": len(subs),
        "active_count": len(active),
        "active_annual_total": round(sum(s.annualized_cents for s in active) / 100, 2),
        "active_monthly_total": round(
            sum(s.annualized_cents for s in active) / 1200, 2
        ),
    }


@mcp.tool()
def get_transactions(
    start: str | None = None,
    end: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Individual transactions matching a filter. Use for drill-down only.

    Prefer the aggregate tools for questions about totals — this returns line
    items and is capped at 200 rows. When the result is capped, ``truncated`` is
    true and ``total_matching`` gives the real count; say so rather than
    summarising the partial set as if it were complete.

    Args:
        start: Inclusive ISO date (YYYY-MM-DD).
        end: Inclusive ISO date (YYYY-MM-DD).
        merchant: Substring match on the normalized merchant name.
        category: Exact category match.
        min_amount: Minimum absolute amount in dollars.
        max_amount: Maximum absolute amount in dollars.
        limit: Rows to return (capped at 200).
    """
    with session(db_path()) as conn:
        return query.list_transactions(
            conn,
            start=start,
            end=end,
            merchant=merchant,
            category=category,
            min_amount=min_amount,
            max_amount=max_amount,
            limit=limit,
        )


@mcp.tool()
def compare_periods(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    limit: int = 25,
) -> dict[str, Any]:
    """Compare spending between two date ranges, biggest movers first.

    Args:
        period_a_start: Start of the earlier period (YYYY-MM-DD).
        period_a_end: End of the earlier period (YYYY-MM-DD).
        period_b_start: Start of the later period (YYYY-MM-DD).
        period_b_end: End of the later period (YYYY-MM-DD).
        limit: Maximum categories to return.
    """
    with session(db_path()) as conn:
        return query.compare_periods(
            conn,
            period_a=(period_a_start, period_a_end),
            period_b=(period_b_start, period_b_end),
            limit=limit,
        )


@mcp.tool()
def spending_by_country(
    start: str | None = None, end: str | None = None, limit: int = 25
) -> dict[str, Any]:
    """Spending grouped by the country each charge happened in.

    Useful for travel: "what did Brazil cost me?". Charges at home appear as a
    single "Home country" bucket so the shares still cover everything.

    Args:
        start: Inclusive ISO date (YYYY-MM-DD).
        end: Inclusive ISO date (YYYY-MM-DD).
        limit: Maximum countries to return.
    """
    with session(db_path()) as conn:
        return query.spending_by_country(conn, start=start, end=end, limit=limit)


@mcp.tool()
def find_trips(min_transactions: int = 3) -> dict[str, Any]:
    """Group foreign transactions into trips, with what each one cost.

    Clusters foreign charges by date to recover journeys without being told
    about them, and attributes foreign transaction fees to the trip they fall
    in. An isolated purchase from a foreign website is not reported as a trip.

    Args:
        min_transactions: Minimum charges before a cluster counts as a trip.

    Returns:
        Trips newest first, with dates, countries, spend and fees.
    """
    with session(db_path()) as conn:
        rows = query.travel_rows(conn)
    trips = detect_trips(rows, min_transactions=min_transactions)
    unresolved = unresolved_locations(rows)

    notes: list[str] = []
    if unresolved:
        listed = ", ".join(f"{code} x{n}" for code, n in unresolved.items())
        notes.append(
            f"{sum(unresolved.values())} charges end in a location code that is "
            f"both a country and a US state ({listed}) and carried no currency to "
            f"settle it, so they were read as domestic. If a trip is missing, "
            f"importing the PDF statement rather than a CSV usually resolves it, "
            f"because the PDF prints the original currency."
        )

    return {
        "trips": [t.to_dict() for t in trips],
        "count": len(trips),
        "total_spent_abroad": round(
            sum(t.spend_cents + t.fee_cents for t in trips) / 100, 2
        ),
        "notes": notes,
    }


@mcp.tool()
def foreign_transaction_costs() -> dict[str, Any]:
    """What spending abroad cost on top of the purchases themselves.

    Two things: the issuer's foreign transaction fees, which are itemised, and
    conversions done at a worse rate than the rest — the signature of dynamic
    currency conversion, where a card machine abroad offers to bill you in your
    home currency and sets its own rate.

    No exchange rates are looked up. Each currency's own transactions provide the
    benchmark, so a charge well off that cluster is the one that was converted by
    somebody else.

    Returns:
        Fee totals, the effective percentage, any poor conversions with what
        each cost, and a per-currency breakdown.
    """
    with session(db_path()) as conn:
        rows = query.travel_rows(conn)
    return foreign_cost_summary(rows)


@mcp.tool()
def suggest_category_rules(limit: int = 20) -> dict[str, Any]:
    """List uncategorized merchants so new categorization rules can be written.

    Categorization inside LedgerLens is deterministic and rule-based, not model-
    based, so that totals never drift between runs. This tool closes the loop:
    it hands you the merchants that no rule matched, ranked by spend, and you
    propose YAML rules the user can paste into their own rules file.

    Args:
        limit: Maximum merchants to return.

    Returns:
        Uncategorized merchants with example descriptors, the path the user's
        rules file should live at, and the YAML shape to write.
    """
    with session(db_path()) as conn:
        pending = query.top_uncategorized(conn, limit=limit)
        existing = _engine().categories

    return {
        **pending,
        "existing_categories": existing,
        "rules_file": str(user_rules_path()),
        "yaml_format": (
            "categories:\n"
            "  - category: Coffee\n"
            "    match:\n"
            '      - "blue bottle|verve|four barrel"\n'
        ),
        "note": (
            "Patterns are case-insensitive regexes matched against the normalized "
            "merchant name. Add 'field: description' to match the raw bank "
            "descriptor instead. After editing the rules file, call "
            "apply_category_rules to update existing transactions."
        ),
    }


@mcp.tool()
def apply_category_rules() -> dict[str, Any]:
    """Re-run categorization over all imported transactions.

    Call this after the user edits their rules file. Re-importing would not do
    it: transaction ids are content hashes, so existing rows are left untouched.
    """
    with session(db_path()) as conn:
        return recategorize(conn, _engine())


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
