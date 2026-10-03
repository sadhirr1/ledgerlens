"""Foreign currency, countries, trips and what conversion costs.

The fixtures plant three trips in three currencies, a known bad conversion, and
two traps: a single order from a foreign website that is not a trip, and a
domestic charge ending ``INDIANAPOLIS IN`` that must not be read as India.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledgerlens.enrich.foreign import (
    ForeignDetail,
    analyse,
    country_name,
    detect_country,
    is_foreign_fee,
    parse_foreign_amount,
    parse_fx_rate,
    strip_fx_fragments,
)
from ledgerlens.enrich.travel import (
    detect_trips,
    find_point_of_sale_conversions,
    foreign_cost_summary,
)
from ledgerlens.ingest import import_file
from ledgerlens.query import spending_by_country, travel_rows

# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "cents", "code"),
    [
        ("125.00 BRL", 12500, "BRL"),
        ("BRL 125.00", 12500, "BRL"),
        ("480,00 MXN", 48000, "MXN"),          # European decimal comma
        ("1,250.00 INR", 125000, "INR"),       # thousands separator
        ("R$ 125,00", 12500, "BRL"),           # symbol
        ("₹1,250.00", 125000, "INR"),
        ("125.00 Brazilian Real", 12500, "BRL"),
    ],
)
def test_foreign_amounts_are_parsed(text, cents, code):
    assert parse_foreign_amount(text) == (cents, code)


def test_zero_decimal_currencies_are_not_divided_by_a_hundred():
    """4500 JPY is 4500 yen. Treating it as 45.00 is a hundredfold error."""
    assert parse_foreign_amount("4500 JPY") == (450000, "JPY")


@pytest.mark.parametrize(
    "text", ["ACME LLC", "ORDER 1234 THE SHOP", "NET 30", "ATM 500", "VAT 20.00"]
)
def test_three_letter_words_are_not_mistaken_for_currencies(text):
    assert parse_foreign_amount(text) is None


@pytest.mark.parametrize(
    ("text", "rate"),
    [
        ("Exchange Rate 5.141500", 5.1415),
        ("@ 5.1415", 5.1415),
        ("CONVERSION RATE: 20.09", 20.09),
    ],
)
def test_rates_are_parsed(text, rate):
    assert parse_fx_rate(text) == pytest.approx(rate)


# --- the state-code collision ---------------------------------------------

def test_currency_decides_the_country():
    assert detect_country("CAFE COFFEE DAY MUMBAI IN", currency="INR") == "IN"


def test_an_ambiguous_code_stays_domestic_without_foreign_evidence():
    """``IN`` is India and Indiana; ``CA`` is Canada and California.

    With no foreign currency on the row there is nothing to tell them apart, and
    reading every Indianapolis purchase as a trip to India is the worse failure.
    """
    assert detect_country("INDIANAPOLIS COLTS SHOP INDIANAPOLIS IN") is None
    assert detect_country("TARGET 00012345 EMERYVILLE CA") is None


def test_an_unambiguous_code_needs_no_corroboration():
    # BR, MX and GB are not US state codes, so they stand on their own.
    assert detect_country("RESTAURANTE SABOR SAO PAULO BR") == "BR"
    assert detect_country("TAQUERIA EL SOL MEXICO CITY MX") == "MX"
    assert detect_country("BOOKSHOP ONLINE LONDON GB") == "GB"


def test_three_letter_and_spelled_out_countries():
    assert detect_country("HOTEL COPACABANA RIO DE JANEIRO BRA") == "BR"
    assert detect_country("SOME MERCHANT BRAZIL") == "BR"
    assert detect_country("A HOTEL SAN JOSE COSTA RICA") == "CR"


def test_country_names_are_human_readable():
    assert country_name("BR") == "Brazil"
    assert country_name(None) == "Unknown"


# --- fees ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "FOREIGN TRANSACTION FEE",
        "Foreign Spend Transaction Fee",
        "INTL TRANSACTION FEE",
        "CROSS BORDER FEE",
        "NON-STERLING TRANSACTION FEE",
        "CURRENCY CONVERSION FEE",
    ],
)
def test_foreign_fees_are_recognised_however_worded(text):
    assert is_foreign_fee(text)


def test_an_ordinary_fee_is_not_a_foreign_fee():
    assert not is_foreign_fee("MONTHLY SERVICE FEE")
    assert not is_foreign_fee("LATE PAYMENT FEE")


# --- the combined analysis -------------------------------------------------

def test_implied_rate_is_preferred_over_the_printed_one():
    """The implied rate is comparable across transactions; a printed one is not.

    Issuers print rates in either direction, so a figure taken at face value
    cannot be compared with the next row's.
    """
    detail = analyse(
        "RESTAURANTE SABOR SAO PAULO BR",
        extra_text="125.00 BRL Exchange Rate 0.194553",
        billed_cents=-2431,
    )
    assert detail.original_currency == "BRL"
    assert detail.fx_rate == pytest.approx(125.00 / 24.31, rel=1e-3)


def test_a_domestic_row_yields_nothing():
    detail = analyse("WHOLEFDS MKT #10238 OAKLAND CA", billed_cents=-8431)
    assert detail == ForeignDetail(is_fee=False)
    assert not detail.is_foreign


def test_fx_fragments_are_stripped_from_merchant_names():
    cleaned = strip_fx_fragments("RESTAURANTE SABOR SAO PAULO BR 125.00 BRL")
    assert "BRL" not in cleaned
    assert "RESTAURANTE SABOR" in cleaned


# --- end to end, against planted ground truth ------------------------------

@pytest.fixture()
def travelled(conn, fixtures):
    result = import_file(conn, fixtures / "travel_statement.pdf")
    assert not result.errors, result.errors
    return conn


def test_the_travel_statement_reconciles(conn, fixtures):
    result = import_file(conn, fixtures / "travel_statement.pdf")
    assert any("reconcile" in n for n in result.notes)


def test_every_trip_is_found(travelled, spec):
    trips = detect_trips(travel_rows(travelled))
    found = {c for t in trips for c in t.countries}
    assert found == {t["country"] for t in spec.TRIPS}


def test_trip_dates_match_what_was_planted(travelled, spec):
    trips = {t.countries[0]: t for t in detect_trips(travel_rows(travelled))}
    for planted in spec.TRIPS:
        trip = trips[planted["country"]]
        days = [c[0] for c in planted["charges"]]
        assert trip.start == date.fromisoformat(min(days))
        assert trip.end >= date.fromisoformat(max(days))


def test_a_single_foreign_website_order_is_not_a_trip(travelled, spec):
    """Buying from abroad is not going abroad."""
    trips = detect_trips(travel_rows(travelled))
    assert "GB" not in {c for t in trips for c in t.countries}
    # ...but it is still recognised as a foreign transaction.
    countries = {c["code"] for c in spending_by_country(travelled)["countries"]}
    assert "GB" in countries


def test_indiana_is_not_india(travelled):
    rows = conn_rows = travel_rows(travelled)
    indiana = [r for r in conn_rows if "INDIANAPOLIS" in str(r["merchant"]).upper()]
    assert indiana, "the fixture plants this trap on purpose"
    assert all(r["country"] is None for r in indiana)
    assert rows is conn_rows


def test_fees_are_attributed_to_the_trip_they_fall_in(travelled):
    trips = detect_trips(travel_rows(travelled))
    assert all(t.fee_cents > 0 for t in trips)


def test_a_point_of_sale_conversion_is_found(travelled, spec):
    """DCC is identified by what is *missing*, not by a bad rate.

    When a cardholder accepts conversion at the till the transaction reaches the
    issuer already in the home currency, so the issuer has nothing to convert and
    prints no local amount and no rate. Mastercard's merchant guide says so
    outright. A detector that compares rates between transactions is therefore
    looking only at the charges that were *not* converted at the till, and can
    never see one.
    """
    planted = next(t["dcc"] for t in spec.TRIPS if t["dcc"])
    _day, descriptor, billed = planted

    found = find_point_of_sale_conversions(travel_rows(travelled))
    assert len(found) == 1, f"one was planted; got {found}"
    suspect = found[0]
    assert suspect.billed_cents == round(billed * 100)
    assert descriptor.upper().startswith(suspect.merchant.upper().split()[0])
    assert suspect.country == "BR"


def test_the_foreign_fee_corroborates_it(travelled):
    """The issuer charges its fee on where a charge was processed, not on what
    currency it arrived in. A fee beside a home-currency charge therefore says
    "this was abroad" while the missing conversion line says "somebody else
    converted it"."""
    suspect = find_point_of_sale_conversions(travel_rows(travelled))[0]
    assert suspect.fee_observed is True
    assert suspect.confidence >= 0.8


def test_a_suspected_conversion_is_never_priced(travelled):
    """There is no local amount to compare against, so any figure would be made
    up. The absence is the signal; it is also the reason it cannot be costed."""
    suspect = find_point_of_sale_conversions(travel_rows(travelled))[0]
    assert suspect.to_dict()["cost"] is None

    summary = foreign_cost_summary(travel_rows(travelled))
    assert summary["cost_of_suspected_conversions"] is None
    assert summary["total_measurable_cost_of_going_abroad"] == summary[
        "foreign_transaction_fees"
    ]


def test_ordinary_foreign_charges_are_not_suspected(travelled, spec):
    """Everything that printed its local amount and rate was converted by the
    card network and must be left alone."""
    found = find_point_of_sale_conversions(travel_rows(travelled))
    assert len(found) == 1
    planted_normal = {c[1] for t in spec.TRIPS for c in t["charges"]}
    assert not any(
        any(n.upper().startswith(f.merchant.upper().split()[0]) for n in planted_normal)
        for f in found
    )


def test_a_foreign_website_order_is_not_a_suspected_conversion(conn, fixtures):
    """A home-currency charge from a foreign online shop looks identical on the
    statement, and nobody chose a currency at a terminal. Scoping detection to
    inside a trip is what separates them."""
    import_file(conn, fixtures / "travel_statement.pdf")
    found = find_point_of_sale_conversions(travel_rows(conn))
    assert not any("Bookshop" in f.merchant for f in found)


def test_cost_summary_reports_only_what_it_can_measure(travelled):
    summary = foreign_cost_summary(travel_rows(travelled))
    assert summary["foreign_transaction_fees"] > 0
    assert summary["suspected_conversion_count"] == 1
    assert set(summary["by_currency"]) == {"BRL", "MXN", "INR", "GBP"}


def test_fees_are_not_counted_as_home_country_spending(travelled):
    labels = {c["country"] for c in spending_by_country(travelled)["countries"]}
    assert "Foreign transaction fees" in labels


def test_country_shares_cover_everything(travelled):
    result = spending_by_country(travelled)
    assert sum(c["share"] for c in result["countries"]) == pytest.approx(100, abs=1.0)
    assert sum(c["spent"] for c in result["countries"]) == pytest.approx(
        result["total_spent"], abs=0.05
    )


# --- the same trips arriving as a CSV --------------------------------------

def test_csv_export_yields_the_same_countries(conn, fixtures):
    import_file(conn, fixtures / "travel_card_2026q4.csv")
    countries = {
        c["code"] for c in spending_by_country(conn)["countries"] if c["code"]
    }
    assert {"BR", "MX", "IN", "GB"} <= countries


def test_merchants_group_across_csv_and_pdf(conn, fixtures):
    """A CSV folds the currency into the descriptor; a PDF puts it on its own
    line. The merchant has to come out the same either way, or the two exports
    of one trip will not group together."""
    import_file(conn, fixtures / "travel_card_2026q4.csv")
    csv_merchants = {
        r["merchant"] for r in conn.execute("SELECT DISTINCT merchant FROM transactions")
    }
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM import_log")
    conn.commit()

    import_file(conn, fixtures / "travel_statement.pdf")
    pdf_merchants = {
        r["merchant"] for r in conn.execute("SELECT DISTINCT merchant FROM transactions")
    }
    assert {"Restaurante Sabor", "Cafe Coffee Day", "Taqueria El Sol"} <= csv_merchants
    assert csv_merchants == pdf_merchants


# --- what a real export actually carries ------------------------------------
#
# PDF statements print the original amount and the rate beside each foreign
# charge. Most CSV exports give only the converted figure. The difference is not
# cosmetic: it decides which questions can be answered at all, and the tool has
# to say which case it is in rather than reporting a reassuring zero.

@pytest.fixture()
def csv_without_fx(conn, fixtures):
    result = import_file(conn, fixtures / "travel_card_no_fx.csv")
    assert not result.errors, result.errors
    return conn


def test_a_csv_without_rates_cannot_check_conversions(csv_without_fx):
    from ledgerlens.enrich.travel import rate_data_availability

    availability = rate_data_availability(travel_rows(csv_without_fx))
    assert availability["foreign_transactions"] > 0
    assert availability["with_original_amount_and_rate"] == 0
    assert availability["can_check_conversions"] is False


def test_unavailable_is_reported_as_unknown_not_as_none_found(csv_without_fx):
    """The distinction this whole project is about.

    The signal for a till conversion is a charge missing its conversion line
    while others have theirs. On an export where *nothing* has one there is no
    contrast to read, and silence would be mistaken for an all-clear.
    """
    summary = foreign_cost_summary(travel_rows(csv_without_fx))
    assert summary["suspected_point_of_sale_conversions"] == []
    assert any("not the same as finding none" in n for n in summary["notes"])


def test_a_pdf_statement_can_check_conversions(travelled):
    from ledgerlens.enrich.travel import rate_data_availability

    availability = rate_data_availability(travel_rows(travelled))
    assert availability["can_check_conversions"] is True
    assert set(availability["checkable_currencies"]) == {"BRL", "MXN", "INR"}
    # The only note here is the caveat attached to the finding itself, not a
    # warning that the check could not run.
    notes = foreign_cost_summary(travel_rows(travelled))["notes"]
    assert not any("cannot be spotted" in n for n in notes)


def test_fees_are_still_found_without_rate_data(csv_without_fx):
    """Fees appear as their own rows, so a plain CSV still reveals them."""
    summary = foreign_cost_summary(travel_rows(csv_without_fx))
    assert summary["foreign_transaction_fees"] > 0


def test_unambiguous_countries_survive_a_plain_csv(csv_without_fx):
    codes = {c["code"] for c in spending_by_country(csv_without_fx)["countries"] if c["code"]}
    assert {"BR", "MX"} <= codes, "BR and MX are not US state codes"


def test_an_ambiguous_country_is_lost_without_a_currency_and_said_so(csv_without_fx):
    """India disappears from a plain CSV, because ``IN`` is also Indiana.

    That is the correct default. It is also how a whole trip goes missing, so
    the count of unresolved codes is reported rather than left silent.
    """
    from ledgerlens.enrich.travel import unresolved_locations

    codes = {c["code"] for c in spending_by_country(csv_without_fx)["countries"] if c["code"]}
    assert "IN" not in codes

    unresolved = unresolved_locations(travel_rows(csv_without_fx))
    assert unresolved.get("IN", 0) >= 6, "the India charges should be counted as unresolved"


def test_the_same_statement_as_pdf_recovers_the_lost_trip(conn, fixtures):
    """The actionable half of the caveat: the PDF carries what the CSV dropped."""
    import_file(conn, fixtures / "travel_card_no_fx.csv")
    csv_countries = {c["code"] for c in spending_by_country(conn)["countries"] if c["code"]}
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM import_log")
    conn.commit()

    import_file(conn, fixtures / "travel_statement.pdf")
    pdf_countries = {c["code"] for c in spending_by_country(conn)["countries"] if c["code"]}

    assert "IN" not in csv_countries
    assert "IN" in pdf_countries


def test_detection_needs_a_statement_that_prints_conversion_lines():
    """A missing conversion line only means something when other lines have one.

    On a statement that never prints them, every foreign charge would otherwise
    look like a till conversion.
    """
    from ledgerlens.enrich.travel import MIN_SIBLINGS_WITH_DETAIL

    assert MIN_SIBLINGS_WITH_DETAIL >= 3


# --- dates are not as precise as they look ---------------------------------
#
# Two separate problems live here. Whether "04/03" is March 4th or April 3rd is
# a format question, settled once per file from the whole column. The harder one
# is that a purchase abroad happens at a moment that is already a different
# calendar date at home, and that a statement's date column mixes transaction
# dates with posting dates. Neither is recoverable, so anything matching on date
# has to tolerate a gap.

def test_a_fee_posting_days_after_its_charge_still_matches(travelled):
    """Fees post after the charge they belong to; the fixture spaces them out."""
    found = find_point_of_sale_conversions(travel_rows(travelled))
    assert found and found[0].fee_observed is True


def test_one_fee_cannot_corroborate_two_charges(travelled):
    """Within the matching window several charges may compete for one fee row.

    Letting a fee be claimed twice would inflate confidence on both.
    """
    from ledgerlens.enrich.travel import _match_fee

    rows = travel_rows(travelled)
    fees = [r for r in rows if r["is_fee"]]
    charge = next(r for r in rows if not r["is_fee"] and r["amount_cents"] < 0)
    expected = abs(int(charge["amount_cents"])) * 0.027

    claimed: set[int] = set()
    first = _match_fee(charge, fees, expected, claimed)
    if first is not None:
        claimed.add(first)
        again = _match_fee(charge, fees, expected, claimed)
        assert again != first, "a claimed fee must not be matched a second time"


def test_the_matching_window_is_wide_enough_to_be_useful_and_narrow_enough_to_mean_something():
    from ledgerlens.enrich.travel import FEE_MATCH_DAYS

    assert 1 <= FEE_MATCH_DAYS <= 5


def test_trip_boundaries_tolerate_a_days_drift(travelled, spec):
    """A charge made late in the evening abroad can land on the previous day at
    home. Trip clustering must not split on that."""
    trips = {t.countries[0]: t for t in detect_trips(travel_rows(travelled))}
    for planted in spec.TRIPS:
        days = [c[0] for c in planted["charges"]]
        trip = trips[planted["country"]]
        assert (trip.start - date.fromisoformat(min(days))).days <= 1
        assert len(trip.countries) == 1, "a day's drift must not merge two trips"
