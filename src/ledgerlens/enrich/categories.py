"""Deterministic, rule-based categorization.

Categorization deliberately does **not** call a language model. The server is
offline, fast and repeatable: the same statement categorized twice gives the
same answer, and a spending total does not drift between runs. The AI client on
the other end is far better used to *suggest new rules* — see the
``suggest_category_rules`` tool — which the user then commits to their own YAML.

Rules load in two layers. The packaged rule pack ships with the project; a
user file at ``$LEDGERLENS_HOME/categories.yaml`` is checked first and wins on
conflict, so local edits are never clobbered by an upgrade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CATEGORY = "Uncategorized"
_PACKAGED_RULES = Path(__file__).resolve().parent.parent / "rules" / "default_categories.yaml"


@dataclass
class Rule:
    category: str
    pattern: re.Pattern[str]
    field: str  # "merchant" | "description"
    source: str  # "user" | "builtin"


class CategoryEngine:
    """Applies an ordered list of regex rules to transactions."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def load(cls, user_path: Path | None = None) -> CategoryEngine:
        """Load user rules (if present) followed by the packaged rule pack."""
        rules: list[Rule] = []
        if user_path and Path(user_path).is_file():
            rules.extend(_parse_rule_file(Path(user_path), source="user"))
        if _PACKAGED_RULES.is_file():
            rules.extend(_parse_rule_file(_PACKAGED_RULES, source="builtin"))
        return cls(rules)

    def categorize(self, merchant: str, description: str = "") -> str:
        """Return the first matching category, or ``Uncategorized``."""
        for rule in self.rules:
            haystack = merchant if rule.field == "merchant" else description
            if haystack and rule.pattern.search(haystack):
                return rule.category
        return DEFAULT_CATEGORY

    @property
    def categories(self) -> list[str]:
        seen: list[str] = []
        for rule in self.rules:
            if rule.category not in seen:
                seen.append(rule.category)
        return sorted(seen)


def _parse_rule_file(path: Path, *, source: str) -> list[Rule]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    entries = raw.get("categories", []) if isinstance(raw, dict) else []
    rules: list[Rule] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category", "")).strip()
        if not category:
            continue
        field = str(entry.get("field", "merchant")).strip().lower()
        if field not in ("merchant", "description"):
            field = "merchant"
        for pattern in entry.get("match", []) or []:
            try:
                compiled = re.compile(str(pattern), re.IGNORECASE)
            except re.error:
                continue  # a bad user regex should not break the whole import
            rules.append(Rule(category, compiled, field, source))
    return rules
