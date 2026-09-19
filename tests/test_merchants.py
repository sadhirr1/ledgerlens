"""Merchant normalization.

Grouping by the raw descriptor produces a report where one coffee shop appears
forty times, so these cases are the difference between a useful summary and an
unreadable one.
"""

from __future__ import annotations

import pytest

from ledgerlens.enrich.merchants import normalize_merchant


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Payment processor prefixes
        ("SQ *BLUE BOTTLE #4412 OAKLAND CA", "Blue Bottle"),
        ("TST* SWEETGREEN 0184 NEW YORK NY", "Sweetgreen"),
        ("PAYPAL *STEAMGAMES 4029357733", "Steamgames"),
        # Transaction-kind noise
        ("POS DEBIT - CVS/PHARMACY #8871 OAKLAND CA", "CVS Pharmacy"),
        ("RECURRING PAYMENT AUTHORIZED ON 05/14 SPOTIFY", "Spotify"),
        # Aliases
        ("AMZN Mktp US*2H4XY9DK3 AMZN.COM/BILL WA", "Amazon"),
        ("AMAZON WEB SERVICES AWS.AMAZON.CO WA", "Amazon Web Services"),
        ("NETFLIX.COM 866-579-7172 CA", "Netflix"),
        ("GITHUB.COM HTTPSGITHUB.C CA", "GitHub"),
        ("UBER *TRIP HELP.UBER.COM CA", "Uber"),
        ("UBER EATS 8005928996 CA", "Uber Eats"),
        ("WHOLEFDS MKT #10238 OAKLAND CA", "Whole Foods"),
        # Trailing junk: store numbers, dates, references, locations
        ("TARGET 00012345 EMERYVILLE CA", "Target"),
        ("SHELL OIL 57442890 SAN JOSE CA", "Shell Oil"),
        ("TRADER JOE'S #182 BERKELEY CA", "Trader Joe's"),
        ("MCDONALD'S F1234 SAN JOSE CA", "McDonald's"),
    ],
)
def test_normalization(raw, expected):
    assert normalize_merchant(raw) == expected


def test_variants_of_one_merchant_collapse_to_one_name():
    variants = [
        "SQ *BLUE BOTTLE #4412 OAKLAND CA",
        "SQ *BLUE BOTTLE #1180 SAN FRANCISCO CA",
        "SQ *BLUE BOTTLE #4412 OAKLAND CA 05/14",
    ]
    assert len({normalize_merchant(v) for v in variants}) == 1


def test_amazon_web_services_does_not_collapse_into_amazon():
    # Two different merchants that share a prefix; conflating them would put
    # cloud hosting in the shopping category.
    assert normalize_merchant("AMAZON WEB SERVICES AWS.AMAZON.CO WA") != normalize_merchant(
        "AMZN Mktp US*2H4XY9DK3"
    )


@pytest.mark.parametrize("raw", ["", "   ", None, "#4412", "05/14"])
def test_degenerate_input_never_returns_empty(raw):
    assert normalize_merchant(raw) == "Unknown"


def test_a_state_code_alone_does_not_erase_the_merchant():
    # "SAFEWAY CA" must not strip down to nothing.
    assert normalize_merchant("SAFEWAY CA") == "Safeway"


def test_known_casing_is_preserved():
    assert normalize_merchant("CVS/PHARMACY #8871") == "CVS Pharmacy"
    assert normalize_merchant("IKEA EMERYVILLE CA") == "IKEA"


def test_normalization_is_idempotent():
    once = normalize_merchant("SQ *BLUE BOTTLE #4412 OAKLAND CA")
    assert normalize_merchant(once) == once
