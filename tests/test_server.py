"""The MCP tool surface.

These tests exercise the tools the way a client does — through the registry,
by name — and check the contract each one advertises: that it is described well
enough for a model to choose it, and that its output is shaped for a context
window rather than a spreadsheet.
"""

from __future__ import annotations

import asyncio

import pytest

from ledgerlens import server

EXPECTED_TOOLS = {
    "import_statements",
    "overview",
    "spending_by_category",
    "spending_by_merchant",
    "monthly_summary",
    "find_subscriptions",
    "get_transactions",
    "compare_periods",
    "suggest_category_rules",
    "apply_category_rules",
}


@pytest.fixture()
def tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_every_tool_is_registered(tools):
    assert set(tools) == EXPECTED_TOOLS


def test_every_tool_is_documented(tools):
    """A model picks tools by their description; an undocumented one is unusable."""
    for name, tool in tools.items():
        assert tool.description, f"{name} has no description"
        assert len(tool.description) > 60, f"{name}'s description is too thin to choose on"


def test_every_tool_advertises_a_schema(tools):
    for name, tool in tools.items():
        assert tool.inputSchema.get("type") == "object", name


def test_import_then_query_round_trip(home, fixtures):
    result = server.import_statements(str(fixtures / "main_checking.csv"))
    assert result["total_inserted"] > 2000
    assert result["database"]["transaction_count"] > 2000

    summary = server.overview()
    assert summary["accounts"][0]["name"] == "Main Checking"

    categories = server.spending_by_category()
    assert any(c["category"] == "Subscriptions" for c in categories["categories"])


def test_import_reports_a_missing_path_without_raising(home):
    assert "error" in server.import_statements("/nonexistent/path")


def test_find_subscriptions_totals_only_active_ones(home, fixtures, spec):
    server.import_statements(str(fixtures / "main_checking.csv"))
    result = server.find_subscriptions(as_of=spec.AS_OF.isoformat())

    assert result["count"] >= len(spec.PLANTED_SUBSCRIPTIONS)
    assert result["active_count"] < result["count"], "the fixture includes a cancelled one"
    assert result["active_annual_total"] > 0
    assert result["active_monthly_total"] == pytest.approx(
        result["active_annual_total"] / 12, abs=0.02
    )


def test_subscription_status_filter(home, fixtures, spec):
    server.import_statements(str(fixtures / "main_checking.csv"))
    cancelled = server.find_subscriptions(
        status="likely_cancelled", as_of=spec.AS_OF.isoformat()
    )
    assert cancelled["subscriptions"]
    assert all(s["status"] == "likely_cancelled" for s in cancelled["subscriptions"])


def test_get_transactions_is_capped_and_says_so(home, fixtures):
    server.import_statements(str(fixtures / "main_checking.csv"))
    result = server.get_transactions(limit=5)
    assert len(result["transactions"]) == 5
    assert result["truncated"] is True
    assert result["total_matching"] > 5


def test_get_transactions_cannot_be_made_to_dump_the_database(home, fixtures):
    server.import_statements(str(fixtures / "main_checking.csv"))
    result = server.get_transactions(limit=999_999)
    assert len(result["transactions"]) <= 200


def test_rule_suggestion_loop_is_actionable(home, fixtures):
    server.import_statements(str(fixtures / "main_checking.csv"))
    result = server.suggest_category_rules()
    assert "yaml_format" in result and "rules_file" in result
    assert result["existing_categories"], "the model needs to know what already exists"


def test_apply_category_rules_reports_what_changed(home, fixtures):
    server.import_statements(str(fixtures / "main_checking.csv"))
    stats = server.apply_category_rules()
    assert stats["examined"] > 0
    assert stats["changed"] == 0, "a fresh import is already categorized"
