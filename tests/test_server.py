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
    "spending_by_country",
    "find_trips",
    "foreign_transaction_costs",
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
    # The SDK renamed this field from inputSchema to input_schema in 2.0; the
    # contract being tested — that every tool declares an object schema — is the
    # same either way.
    for name, tool in tools.items():
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        assert schema is not None, f"{name} advertises no input schema"
        assert schema.get("type") == "object", name


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


# --- the travel tools ------------------------------------------------------

def test_spending_by_country_tool(home, fixtures):
    server.import_statements(str(fixtures / "travel_statement.pdf"))
    result = server.spending_by_country()
    codes = {c["code"] for c in result["countries"] if c["code"]}
    assert {"BR", "MX", "IN"} <= codes
    assert result["total_spent"] > 0


def test_find_trips_tool(home, fixtures, spec):
    server.import_statements(str(fixtures / "travel_statement.pdf"))
    result = server.find_trips()
    assert result["count"] == len(spec.TRIPS)
    assert result["total_spent_abroad"] > 0
    assert all(t["fees"] > 0 for t in result["trips"])


def test_foreign_transaction_costs_tool(home, fixtures):
    server.import_statements(str(fixtures / "travel_statement.pdf"))
    result = server.foreign_transaction_costs()
    assert result["foreign_transaction_fees"] > 0
    assert len(result["poor_conversions"]) == 1
    assert result["poor_conversions"][0]["worse_by_percent"] > 3


def test_travel_tools_are_quiet_on_a_domestic_only_database(home, fixtures):
    """Someone who never leaves the country should get empty results, not errors."""
    server.import_statements(str(fixtures / "main_checking.csv"))
    assert server.find_trips()["count"] == 0
    assert server.foreign_transaction_costs()["foreign_transaction_fees"] == 0
    countries = server.spending_by_country()["countries"]
    assert [c["country"] for c in countries] == ["Home country"]
