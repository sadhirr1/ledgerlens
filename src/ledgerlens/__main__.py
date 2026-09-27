"""Command line entry point.

The MCP server is the point of the project, but a CLI makes it possible to
verify an import and eyeball the results without wiring up a client first —
which is also how the README demo is produced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ledgerlens import __version__, query
from ledgerlens.config import db_path, user_rules_path
from ledgerlens.db import session
from ledgerlens.enrich.categories import CategoryEngine
from ledgerlens.enrich.recurring import detect_subscriptions
from ledgerlens.ingest import import_file, import_folder, recategorize


def _dump(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_import(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if not target.exists():
        print(f"error: {target} does not exist", file=sys.stderr)
        return 1
    engine = CategoryEngine.load(user_rules_path())
    with session(db_path()) as conn:
        if target.is_dir():
            results = import_folder(conn, target, engine=engine, force=args.force)
        else:
            results = [import_file(conn, target, engine=engine, force=args.force)]

    for r in results:
        state = "skipped (already imported)" if r.already_imported else (
            f"{r.rows_inserted} imported, {r.rows_skipped} skipped"
        )
        print(f"{Path(r.path).name:<34} {state}")
        for note in r.notes:
            print(f"  \u2713 {note}")
        for warning in r.warnings:
            print(f"  ! {warning}")
        for err in r.errors[:3]:
            print(f"  x {err}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with session(db_path()) as conn:
        _dump(query.overview(conn))
    return 0


def cmd_subscriptions(args: argparse.Namespace) -> int:
    from datetime import date

    with session(db_path()) as conn:
        rows = query.all_transactions_for_recurrence(conn)
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    subs = detect_subscriptions(rows, as_of=as_of, min_confidence=args.min_confidence)
    if args.json:
        _dump([s.to_dict() for s in subs])
        return 0

    if not subs:
        print("No recurring charges detected.")
        return 0

    width = max(len(s.merchant) for s in subs)
    print(f"{'MERCHANT'.ljust(width)}  {'CADENCE':<10} {'AMOUNT':>9} {'ANNUAL':>10}  STATUS")
    for s in subs:
        print(
            f"{s.merchant.ljust(width)}  {s.cadence:<10} "
            f"{s.typical_amount_cents / 100:>9,.2f} "
            f"{s.annualized_cents / 100:>10,.2f}  {s.status}"
        )
    active = [s for s in subs if s.status == "active"]
    total = sum(s.annualized_cents for s in active) / 100
    print(f"\n{len(active)} active — {total:,.2f}/year ({total / 12:,.2f}/month)")
    return 0


def cmd_categories(args: argparse.Namespace) -> int:
    with session(db_path()) as conn:
        if args.apply:
            _dump(recategorize(conn, CategoryEngine.load(user_rules_path())))
        else:
            _dump(query.top_uncategorized(conn, limit=args.limit))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from ledgerlens.server import main as serve

    serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledgerlens",
        description="Query your bank statement exports locally, over MCP.",
    )
    parser.add_argument("--version", action="version", version=f"ledgerlens {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import statement CSVs")
    p_import.add_argument("path", help="folder of CSVs, or a single CSV file")
    p_import.add_argument("--force", action="store_true", help="re-import known files")
    p_import.set_defaults(func=cmd_import)

    p_status = sub.add_parser("status", help="show what is loaded")
    p_status.set_defaults(func=cmd_status)

    p_subs = sub.add_parser("subscriptions", help="list detected recurring charges")
    p_subs.add_argument("--min-confidence", type=float, default=0.55)
    p_subs.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="judge active/cancelled as at this date instead of today",
    )
    p_subs.add_argument("--json", action="store_true")
    p_subs.set_defaults(func=cmd_subscriptions)

    p_cat = sub.add_parser("categories", help="inspect or re-apply categorization")
    p_cat.add_argument("--apply", action="store_true", help="re-categorize existing rows")
    p_cat.add_argument("--limit", type=int, default=20)
    p_cat.set_defaults(func=cmd_categories)

    p_serve = sub.add_parser("serve", help="run the MCP server over stdio")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
