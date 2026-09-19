"""Recurring-charge detection, asserted against planted ground truth.

The fixtures contain subscriptions with a known cadence, amount and end date,
so these tests check that the detector finds what is actually there rather than
that it still produces whatever it produced when the test was written.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ledgerlens.enrich.recurring import detect_subscriptions
from ledgerlens.ingest import import_file
from ledgerlens.query import all_transactions_for_recurrence


def _series(start: date, count: int, step_days: int, amount: int, merchant: str, jitter=None):
    jitter = jitter or [0] * count
    return [
        {
            "posted_on": start + timedelta(days=i * step_days + jitter[i]),
            "amount_cents": amount,
            "merchant": merchant,
            "category": "Subscriptions",
        }
        for i in range(count)
    ]


# --- unit-level behaviour ---------------------------------------------------

def test_clean_monthly_series_is_detected():
    rows = _series(date(2026, 1, 5), 6, 30, -1599, "Netflix")
    subs = detect_subscriptions(rows, as_of=date(2026, 6, 10))
    assert len(subs) == 1
    assert subs[0].cadence == "monthly"
    assert subs[0].typical_amount_cents == 1599
    assert subs[0].annualized_cents == pytest.approx(19467, abs=200)


def test_billing_drift_around_weekends_does_not_break_detection():
    jitter = [0, 1, -2, 0, 2, -1, 0, 1]
    rows = _series(date(2026, 1, 5), 8, 30, -999, "Spotify", jitter)
    subs = detect_subscriptions(rows, as_of=date(2026, 8, 10))
    assert subs and subs[0].cadence == "monthly"


def test_a_skipped_month_is_tolerated():
    rows = _series(date(2025, 1, 6), 8, 30, -1299, "Hulu")
    del rows[4]  # a failed payment leaves a double-length gap
    subs = detect_subscriptions(rows, as_of=date(2025, 9, 10))
    assert subs, "one missed payment should not hide an otherwise clean series"


def test_irregular_spending_is_not_a_subscription():
    days = [0, 3, 4, 11, 12, 13, 29, 51, 52, 80]
    rows = [
        {
            "posted_on": date(2026, 1, 1) + timedelta(days=d),
            "amount_cents": -450 - d,
            "merchant": "Blue Bottle",
            "category": "Dining",
        }
        for d in days
    ]
    assert detect_subscriptions(rows, as_of=date(2026, 4, 1)) == []


def test_two_occurrences_are_not_enough():
    rows = _series(date(2026, 1, 5), 2, 30, -1599, "Netflix")
    assert detect_subscriptions(rows, as_of=date(2026, 3, 10)) == []


def test_income_is_ignored():
    rows = _series(date(2026, 1, 15), 6, 14, 284166, "Acme Corp")
    assert detect_subscriptions(rows, as_of=date(2026, 4, 1)) == []


def test_cancelled_subscription_is_flagged():
    rows = _series(date(2025, 1, 3), 6, 30, -2499, "Planet Fitness")
    # Last charge is June 2025; six months later it is plainly gone.
    subs = detect_subscriptions(rows, as_of=date(2025, 12, 20))
    assert subs and subs[0].status == "likely_cancelled"


def test_recently_due_subscription_is_still_active():
    rows = _series(date(2026, 1, 5), 6, 30, -1599, "Netflix")
    last = rows[-1]["posted_on"]
    subs = detect_subscriptions(rows, as_of=last + timedelta(days=33))
    assert subs and subs[0].status == "active"


def test_price_increase_is_reported_not_penalised():
    rows = _series(date(2025, 1, 5), 6, 30, -1099, "Spotify")
    for row in rows[3:]:
        row["amount_cents"] = -1199
    subs = detect_subscriptions(rows, as_of=date(2025, 6, 20))
    assert subs and subs[0].price_changed
    assert subs[0].typical_amount_cents == 1199, "the current price, not the average"


def test_variable_amount_bill_is_still_recurring():
    rows = _series(date(2025, 1, 15), 8, 30, -5000, "PG&E")
    for i, row in enumerate(rows):
        row["amount_cents"] = -(4000 + i * 900)
    subs = detect_subscriptions(rows, as_of=date(2025, 9, 1))
    assert subs and subs[0].amount_varies


def test_one_merchant_billing_two_subscriptions_is_split():
    rows = _series(date(2025, 1, 4), 6, 30, -299, "Apple")
    rows += _series(date(2025, 1, 19), 6, 30, -1099, "Apple")
    subs = detect_subscriptions(rows, as_of=date(2025, 6, 25))
    amounts = sorted(s.typical_amount_cents for s in subs)
    assert amounts == [299, 1099], "each price point is its own subscription"


def test_as_of_is_explicit_so_results_are_stable():
    rows = _series(date(2025, 1, 5), 6, 30, -1599, "Netflix")
    early = detect_subscriptions(rows, as_of=date(2025, 6, 10))
    late = detect_subscriptions(rows, as_of=date(2026, 6, 10))
    assert early[0].status == "active"
    assert late[0].status == "likely_cancelled"


# --- end to end against the fixtures ---------------------------------------

def test_planted_subscriptions_are_all_found(conn, fixtures, spec):
    import_file(conn, fixtures / "main_checking.csv")
    rows = all_transactions_for_recurrence(conn)
    subs = detect_subscriptions(rows, as_of=spec.AS_OF)

    for name, truth in spec.PLANTED_SUBSCRIPTIONS.items():
        matches = [s for s in subs if s.merchant.startswith(name)]
        assert matches, f"{name} was planted in the fixture but not detected"
        found = matches[0]
        assert found.cadence == truth["cadence"], f"{name}: wrong cadence"
        assert found.status == truth["status"], f"{name}: wrong status"
        if "amount" in truth:
            assert found.typical_amount_cents == round(truth["amount"] * 100), name
        if truth.get("price_changed"):
            assert found.price_changed, f"{name}: price change missed"
        if truth.get("amount_varies"):
            assert found.amount_varies, f"{name}: variable amount missed"


def test_no_false_positives_among_ordinary_spending(conn, fixtures, spec):
    """Precision, not just recall.

    The fixture contains twelve merchants of routine spending — a lunch place
    visited 149 times, a supermarket, a petrol station. Given six candidate
    cadences and hundreds of transactions, some subset of those receipts will
    always *look* periodic. A report that tells someone they subscribe to their
    corner shop is worse than one that misses something, so this asserts the
    detected set is exactly the planted set.
    """
    import_file(conn, fixtures / "main_checking.csv")
    subs = detect_subscriptions(all_transactions_for_recurrence(conn), as_of=spec.AS_OF)

    detected = {s.merchant for s in subs}
    planted = {
        next(s.merchant for s in subs if s.merchant.startswith(name))
        for name in spec.PLANTED_SUBSCRIPTIONS
        if any(s.merchant.startswith(name) for s in subs)
    }
    assert detected == planted, f"false positives: {sorted(detected - planted)}"


def test_payroll_is_not_reported_as_a_subscription(conn, fixtures, spec):
    import_file(conn, fixtures / "main_checking.csv")
    subs = detect_subscriptions(all_transactions_for_recurrence(conn), as_of=spec.AS_OF)
    assert not any("Acme" in s.merchant for s in subs)


def test_annual_total_counts_only_active_subscriptions(conn, fixtures, spec):
    import_file(conn, fixtures / "main_checking.csv")
    subs = detect_subscriptions(all_transactions_for_recurrence(conn), as_of=spec.AS_OF)
    active = [s for s in subs if s.status == "active"]
    cancelled = [s for s in subs if s.status == "likely_cancelled"]
    assert active and cancelled, "the fixture contains both, so both must appear"
    assert all(s.annualized_cents > 0 for s in subs)
