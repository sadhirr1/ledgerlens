"""Configuration and filesystem locations.

Everything LedgerLens writes lives under a single XDG-respecting data
directory, overridable with ``LEDGERLENS_HOME`` (which the test-suite uses to
keep runs hermetic).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def data_home() -> Path:
    """Directory holding the database and user rule overrides."""
    if override := os.environ.get("LEDGERLENS_HOME"):
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ledgerlens"


def db_path() -> Path:
    return data_home() / "ledgerlens.db"


def user_rules_path() -> Path:
    return data_home() / "categories.yaml"


@dataclass
class DialectOverride:
    """User-supplied hints that bypass auto-detection for a given import.

    Auto-detection handles the common cases; this exists for the bank whose
    export is strange enough to defeat it. Any field left ``None`` is still
    auto-detected, so an override can be partial.
    """

    date_column: str | None = None
    amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    description_column: str | None = None
    date_format: str | None = None
    day_first: bool | None = None
    negative_is_outflow: bool | None = None
    account_name: str | None = None
    currency: str | None = None
    pdf_password: str | None = None
    skip_rows: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DialectOverride:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(**kwargs, extra=extra)


def load_override(folder: Path) -> DialectOverride | None:
    """Load ``ledgerlens.yaml`` from ``folder`` if the user placed one there."""
    candidate = Path(folder) / "ledgerlens.yaml"
    if not candidate.is_file():
        return None
    with candidate.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        return None
    return DialectOverride.from_dict(raw)
