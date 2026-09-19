"""Aggregation queries and, above all, the token budget.

A tool that can flood a context window eventually will, so the caps and the
truncation flags are treated as behaviour worth testing rather than as
implementation detail.
"""

from __future__ import annotations

import pytest

from ledgerlens import query
from ledgerlens.query import DEFAULT_LIMIT, MAX_LIMIT


def test_overview_describes_the_database(loaded):
    result = query.overview(loaded)
    assert result["transaction_count"] > 2000
    assert result["date_range"]["start"] < result["date_range"]["end"]
    assert result["total_out"] > 0 and result["total_in"] > 0
    assert result["accounts"][0]["name"] == "Main Checking"


def test_category_totals_are_positive_and_sum_to_the_total(loaded):
    result = query.spending_by_category(loaded)
    assert result["categories"], "the fixture should categorize into something"
    assert all(c["spent"] > 0 for c in result["categories"]), (
        "spending totals are reported as positive magnitudes"
    )
    assert sum(c["spent"] for c in result["categories"]) == pytest.approx(
        result["total_spent"], abs=0.05
    )
    assert sum(c["share"] for c in result["categories"]) == pytest.approx(100, abs=1.0)


def test_subscriptions_category_contains_the_planted_services(loaded):
    result = query.spending_by_merchant(loaded, category="Subscriptions")
    names = {m["merchant"] for m in result["merchants"]}
    assert {"Netflix", "Spotify"} <= names


def test_date_window_is_respected(loaded):
    result = query.list_transactions(
        loaded, start="2026-01-01", end="2026-01-31", limit=MAX_LIMIT
    )
    assert result["transactions"]
    assert all(t["date"].startswith("2026-01") for t in result["transactions"])


def test_monthly_summary_is_ordered_and_averaged(loaded):
    result = query.monthly_summary(loaded, limit=12)
    months = [m["month"] for m in result["months"]]
    assert months == sorted(months, reverse=True), "most recent month first"
    assert result["average_monthly_spend"] > 0


def test_compare_periods_ranks_by_absolute_movement(loaded):
    result = query.compare_periods(
        loaded,
        period_a=("2025-01-01", "2025-06-30"),
        period_b=("2026-01-01", "2026-06-30"),
    )
    changes = [abs(c["change"]) for c in result["changes"]]
    assert changes == sorted(changes, reverse=True)


# --- the token budget ------------------------------------------------------

def test_transactions_are_capped_by_default(loaded):
    result = query.list_transactions(loaded)
    assert len(result["transactions"]) <= DEFAULT_LIMIT


def test_an_absurd_limit_is_clamped(loaded):
    result = query.list_transactions(loaded, limit=100_000)
    assert len(result["transactions"]) <= MAX_LIMIT


def test_truncation_is_announced_with_the_real_total(loaded):
    result = query.list_transactions(loaded, limit=10)
    assert result["truncated"] is True
    assert result["returned"] == 10
    assert result["total_matching"] > 10, (
        "the model must be able to say 'showing 10 of N' rather than "
        "summarising a slice as though it were everything"
    )


def test_a_complete_result_is_not_marked_truncated(loaded):
    result = query.list_transactions(
        loaded, merchant="Netflix", start="2026-01-01", end="2026-03-31", limit=MAX_LIMIT
    )
    assert result["truncated"] is False
    assert result["returned"] == result["total_matching"]


def test_merchant_listing_reports_how_many_were_omitted(loaded):
    result = query.spending_by_merchant(loaded, limit=3)
    assert len(result["merchants"]) == 3
    assert result["truncated"] is True
    assert result["total_merchants"] > 3


def test_descriptions_are_trimmed(loaded):
    result = query.list_transactions(loaded, limit=MAX_LIMIT)
    assert all(len(t["description"]) <= 80 for t in result["transactions"])


def test_empty_database_returns_a_usable_shape(conn):
    result = query.overview(conn)
    assert result["transaction_count"] == 0
    assert result["total_out"] == 0
    assert query.spending_by_category(conn)["categories"] == []
    assert query.list_transactions(conn)["transactions"] == []


def test_amounts_cross_the_boundary_as_dollars_not_cents(loaded):
    result = query.list_transactions(loaded, merchant="Netflix", limit=5)
    assert all(abs(t["amount"]) == 15.99 for t in result["transactions"])


def test_uncategorized_worklist_is_ranked_by_spend(loaded):
    result = query.top_uncategorized(loaded, limit=10)
    spends = [m["spent"] for m in result["merchants"]]
    assert spends == sorted(spends, reverse=True)
