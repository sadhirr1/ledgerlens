"""Configuration: data locations and the per-folder dialect override.

``ledgerlens.yaml`` is the documented escape hatch for a bank strange enough to
defeat auto-detection, so it needs to actually work — including the partial
case, where the user pins one column and leaves the rest to be detected.
"""

from __future__ import annotations

import os

from ledgerlens.config import DialectOverride, data_home, db_path, load_override
from ledgerlens.ingest import import_file


def test_data_home_follows_the_environment(home):
    assert data_home() == home
    assert db_path().parent == home


def test_data_home_falls_back_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("LEDGERLENS_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert data_home() == tmp_path / "ledgerlens"


def test_data_home_defaults_under_home(monkeypatch):
    monkeypatch.delenv("LEDGERLENS_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert data_home().parts[-3:] == (".local", "share", "ledgerlens")


def test_no_override_file_is_fine(tmp_path):
    assert load_override(tmp_path) is None


def test_override_is_parsed(tmp_path):
    (tmp_path / "ledgerlens.yaml").write_text(
        "account_name: Barclays Current\nday_first: true\ncurrency: GBP\nskip_rows: 2\n",
        encoding="utf-8",
    )
    override = load_override(tmp_path)
    assert override.account_name == "Barclays Current"
    assert override.day_first is True
    assert override.currency == "GBP"
    assert override.skip_rows == 2
    assert override.date_column is None, "unspecified fields stay auto-detected"


def test_unknown_keys_are_kept_but_do_not_crash(tmp_path):
    (tmp_path / "ledgerlens.yaml").write_text(
        "account_name: Test\nfuture_option: 42\n", encoding="utf-8"
    )
    override = load_override(tmp_path)
    assert override.account_name == "Test"
    assert override.extra == {"future_option": 42}


def test_malformed_override_is_ignored(tmp_path):
    (tmp_path / "ledgerlens.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    assert load_override(tmp_path) is None


def test_override_renames_the_account_on_import(conn, fixtures, tmp_path):
    import shutil

    shutil.copy(fixtures / "main_checking.csv", tmp_path / "main_checking.csv")
    override = DialectOverride(account_name="Barclays Current", currency="GBP")
    import_file(conn, tmp_path / "main_checking.csv", override=override)

    row = conn.execute("SELECT name, currency FROM accounts").fetchone()
    assert row["name"] == "Barclays Current"
    assert row["currency"] == "GBP"


def test_override_forces_an_ambiguous_date_to_day_first(conn, fixtures):
    """The documented fix for the ambiguous-dates warning."""
    default = import_file(conn, fixtures / "ambiguous_dates.csv")
    assert any("ambiguous" in w.lower() for w in default.warnings)
    months = {
        r["posted_on"][:7]
        for r in conn.execute("SELECT posted_on FROM transactions").fetchall()
    }

    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM import_log")
    conn.commit()

    forced = import_file(
        conn, fixtures / "ambiguous_dates.csv", override=DialectOverride(day_first=True)
    )
    assert forced.rows_inserted > 0
    forced_months = {
        r["posted_on"][:7]
        for r in conn.execute("SELECT posted_on FROM transactions").fetchall()
    }
    assert forced_months != months, "the override should change how dates are read"


def test_folder_override_is_picked_up_by_import(conn, fixtures, tmp_path):
    import shutil

    shutil.copy(fixtures / "main_checking.csv", tmp_path / "main_checking.csv")
    (tmp_path / "ledgerlens.yaml").write_text(
        "account_name: Shared Account\n", encoding="utf-8"
    )

    from ledgerlens.ingest import import_folder

    results = import_folder(conn, tmp_path)
    assert len(results) == 1, "the yaml file itself must not be treated as a statement"
    assert results[0].account == "Shared Account"


def test_environment_isolation_actually_isolates(home):
    assert str(home) == os.environ["LEDGERLENS_HOME"]
    assert str(db_path()).startswith(str(home))
