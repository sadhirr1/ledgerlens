"""Shared test fixtures.

Every test runs against a temporary ``LEDGERLENS_HOME`` so the suite never
touches a real database, and the generator module is imported directly so that
assertions compare against the ground truth used to build the fixtures rather
than against hand-copied expectations that can drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_generator():
    spec = importlib.util.spec_from_file_location("fixture_spec", FIXTURES / "generate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fixture_spec"] = module
    spec.loader.exec_module(module)
    return module


fixture_spec = _load_generator()


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    """Isolated data directory for one test."""
    target = tmp_path / "ledgerlens-home"
    target.mkdir()
    monkeypatch.setenv("LEDGERLENS_HOME", str(target))
    return target


@pytest.fixture()
def conn(home):
    """An open, empty LedgerLens database."""
    from ledgerlens.db import connect

    connection = connect(home / "test.db")
    yield connection
    connection.close()


@pytest.fixture()
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture()
def spec():
    """The fixture generator module: ground truth for the planted data."""
    return fixture_spec


@pytest.fixture()
def loaded(conn, fixtures):
    """A database with the main checking fixture imported."""
    from ledgerlens.ingest import import_file

    result = import_file(conn, fixtures / "main_checking.csv")
    assert result.rows_inserted > 0, result.errors[:3]
    return conn
