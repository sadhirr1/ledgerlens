"""The command line interface.

The CLI is how someone verifies an import without wiring up an MCP client
first, and it is what the README demo shows — so it is worth testing rather
than assuming.
"""

from __future__ import annotations

import json

import pytest

from ledgerlens.__main__ import build_parser, main


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def test_import_then_status(home, fixtures, capsys):
    assert run(["import", str(fixtures / "main_checking.csv")]) == 0
    assert "imported" in capsys.readouterr().out

    assert run(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transaction_count"] > 2000


def test_import_reports_warnings_to_the_user(home, fixtures, capsys):
    run(["import", str(fixtures / "ambiguous_dates.csv")])
    out = capsys.readouterr().out
    assert "!" in out and "ambiguous" in out.lower(), (
        "a low-confidence guess must be visible in the terminal, not just the log"
    )


def test_importing_a_folder(home, fixtures, capsys):
    assert run(["import", str(fixtures)]) == 0
    assert capsys.readouterr().out.count("imported") >= 7


def test_missing_path_exits_nonzero(home, capsys):
    assert run(["import", "/nope/nothing.csv"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_subscriptions_table(home, fixtures, spec, capsys):
    run(["import", str(fixtures / "main_checking.csv")])
    capsys.readouterr()

    assert run(["subscriptions", "--as-of", spec.AS_OF.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "Netflix" in out and "MERCHANT" in out
    assert "/year" in out and "/month" in out


def test_subscriptions_json(home, fixtures, spec, capsys):
    run(["import", str(fixtures / "main_checking.csv")])
    capsys.readouterr()

    assert run(["subscriptions", "--as-of", spec.AS_OF.isoformat(), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {s["merchant"] for s in payload} >= {"Netflix", "Spotify"}


def test_subscriptions_on_an_empty_database(home, capsys):
    assert run(["subscriptions"]) == 0
    assert "No recurring charges" in capsys.readouterr().out


def test_categories_lists_then_applies(home, fixtures, capsys):
    run(["import", str(fixtures / "main_checking.csv")])
    capsys.readouterr()

    assert run(["categories", "--limit", "5"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert len(listing["merchants"]) <= 5

    assert run(["categories", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["examined"] > 0


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert "ledgerlens" in capsys.readouterr().out


def test_no_subcommand_is_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code != 0


def test_main_propagates_the_exit_code(home, monkeypatch):
    monkeypatch.setattr("sys.argv", ["ledgerlens", "import", "/nope/nothing.csv"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
