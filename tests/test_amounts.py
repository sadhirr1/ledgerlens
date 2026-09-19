"""Money parsing and direction inference."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ledgerlens.ingest.amounts import (
    AmountParseError,
    infer_sign_plan,
    parse_magnitude,
    signed_cents,
    to_cents,
)


@pytest.mark.parametrize(
    ("raw", "expected", "negative"),
    [
        ("12.50", Decimal("12.50"), False),
        ("-12.50", Decimal("12.50"), True),
        ("12.50-", Decimal("12.50"), True),          # trailing minus
        ("(1,234.56)", Decimal("1234.56"), True),    # accounting parentheses
        ("$1,234.56", Decimal("1234.56"), False),
        ("£99.00", Decimal("99.00"), False),
        ("1.234,56", Decimal("1234.56"), False),     # European
        ("12,50", Decimal("12.50"), False),          # European, no thousands
        ("1,234", Decimal("1234"), False),           # US thousands separator
        ("  45.00 DR ", Decimal("45.00"), False),    # direction stripped here
        ("1 234.56", Decimal("1234.56"), False),     # space as thousands sep
    ],
)
def test_parse_magnitude(raw, expected, negative):
    value, was_negative = parse_magnitude(raw)
    assert value == expected
    assert was_negative is negative


@pytest.mark.parametrize("raw", ["", "   ", None, "abc", "--"])
def test_parse_magnitude_rejects_junk(raw):
    with pytest.raises(AmountParseError):
        parse_magnitude(raw)


def test_to_cents_rounds_half_up_without_float_error():
    assert to_cents(Decimal("0.005")) == 1
    assert to_cents(Decimal("12.345")) == 1235
    # The classic float trap: 1.115 is not representable in binary.
    assert to_cents(Decimal("1.115")) == 112


def test_debit_credit_columns_are_authoritative():
    plan = infer_sign_plan(has_debit_credit=True)
    assert plan.mode == "debit_credit"
    assert signed_cents(plan, debit_raw="24.99", credit_raw="") == -2499
    assert signed_cents(plan, debit_raw="", credit_raw="2841.66") == 284166


def test_mixed_signs_read_negative_as_outflow():
    samples = [(Decimal("10"), True), (Decimal("20"), True), (Decimal("500"), False)]
    plan = infer_sign_plan(has_debit_credit=False, signed_samples=samples)
    assert plan.mode == "signed"
    assert plan.negative_is_outflow is True
    assert signed_cents(plan, amount_raw="-15.99") == -1599
    assert signed_cents(plan, amount_raw="2841.66") == 284166


def test_type_column_carries_direction_when_amounts_are_unsigned():
    samples = [(Decimal("10"), False), (Decimal("20"), False), (Decimal("30"), False)]
    plan = infer_sign_plan(
        has_debit_credit=False, signed_samples=samples, type_samples=["DR", "DR", "CR"]
    )
    assert plan.mode == "typed"
    assert signed_cents(plan, amount_raw="24.99", type_raw="DR") == -2499
    assert signed_cents(plan, amount_raw="24.99", type_raw="CR") == 2499


def test_unsigned_with_no_type_column_assumes_spend_but_flags_low_confidence():
    samples = [(Decimal("10"), False), (Decimal("20"), False)]
    plan = infer_sign_plan(has_debit_credit=False, signed_samples=samples)
    assert plan.mode == "assume_outflow"
    assert plan.confidence < 0.7, "an assumption this consequential must be flagged"
    assert signed_cents(plan, amount_raw="54.67") == -5467


def test_user_override_wins_over_inference():
    samples = [(Decimal("10"), True), (Decimal("20"), False)]
    plan = infer_sign_plan(has_debit_credit=False, signed_samples=samples, override=False)
    assert plan.negative_is_outflow is False
    assert signed_cents(plan, amount_raw="15.00") == -1500
