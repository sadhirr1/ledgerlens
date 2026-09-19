"""The pre-commit guard that keeps real statements out of the repository.

A safety net nobody has tested is not a safety net.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_no_real_data import looks_like_a_statement, main  # noqa: E402

REAL_LOOKING = (
    "Date,Description,Amount,Balance\n"
    "2026-01-02,ACME CORP PAYROLL,2841.66,5000.00\n"
    "2026-01-03,TESCO STORES 4471998812,-42.10,4957.90\n"
)


def test_a_real_looking_statement_is_blocked(tmp_path):
    path = tmp_path / "downloads.csv"
    path.write_text(REAL_LOOKING, encoding="utf-8")
    assert looks_like_a_statement(path)
    assert main([str(path)]) == 1


def test_banking_interchange_formats_are_blocked(tmp_path):
    path = tmp_path / "export.ofx"
    path.write_text("<OFX></OFX>", encoding="utf-8")
    assert main([str(path)]) == 1


def test_the_projects_own_fixtures_are_exempt():
    assert main(["tests/fixtures/main_checking.csv"]) == 0


def test_ordinary_files_pass(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Hello\n\nAmount, date and description are words.\n", encoding="utf-8")
    code = tmp_path / "thing.py"
    code.write_text("amount = 1\ndate = 2\ndescription = 3\n", encoding="utf-8")
    assert main([str(readme), str(code)]) == 0


def test_a_ledger_header_alone_is_not_enough(tmp_path):
    """Documentation examples mention these columns; don't cry wolf."""
    path = tmp_path / "example.csv"
    path.write_text("Date,Description,Amount\n2026-01-02,COFFEE,-4.50\n", encoding="utf-8")
    assert looks_like_a_statement(path) is None


@pytest.mark.parametrize(
    "identifier",
    ["GB29NWBK60161331926819", "4111 1111 1111 1111", "60161331926819"],
)
def test_account_identifiers_trigger_the_guard(tmp_path, identifier):
    path = tmp_path / "data.csv"
    path.write_text(
        f"Date,Description,Amount\n2026-01-02,TRANSFER {identifier},-10.00\n",
        encoding="utf-8",
    )
    assert looks_like_a_statement(path)


def test_the_repository_is_clean():
    """The hook, run over everything this repo actually tracks."""
    root = Path(__file__).resolve().parent.parent
    candidates = [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".csv", ".tsv", ".txt", ".ofx", ".qfx", ".qif"}
        and ".git" not in p.parts
    ]
    assert main(candidates) == 0
