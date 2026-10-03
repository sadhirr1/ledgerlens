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
from ledgerlens.enrich.travel import detect_trips, find_poor_conversions, foreign_cost_summary
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


def test_the_bad_conversion_is_found(travelled, spec):
    planted = next(t["poor"] for t in spec.TRIPS if t["poor"])
    _day, descriptor, _local, bad_rate = planted

    poor = find_poor_conversions(travel_rows(travelled))
    assert len(poor) == 1, f"exactly one conversion was planted; got {poor}"
    found = poor[0]
    assert found.rate == pytest.approx(bad_rate, rel=1e-3)
    assert descriptor.upper().startswith(found.merchant.upper().split()[0])
    assert found.extra_cost_cents > 0


def test_good_conversions_are_left_alone(travelled):
    """Everything converted at the ordinary rate must stay unflagged."""
    poor = find_poor_conversions(travel_rows(travelled))
    assert {p.currency for p in poor} == {"BRL"}


def test_a_currency_with_too_few_samples_is_not_judged(travelled):
    """One GBP transaction cannot establish what a normal GBP rate looks like."""
    poor = find_poor_conversions(travel_rows(travelled))
    assert "GBP" not in {p.currency for p in poor}


def test_cost_summary_adds_up(travelled):
    summary = foreign_cost_summary(travel_rows(travelled))
    assert summary["foreign_transaction_fees"] > 0
    assert summary["lost_to_poor_conversions"] > 0
    assert summary["total_cost_of_going_abroad"] == pytest.approx(
        summary["foreign_transaction_fees"] + summary["lost_to_poor_conversions"], abs=0.02
    )
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


def test_unavailable_is_reported_as_unknown_not_as_zero(csv_without_fx):
    """The distinction this whole project is about.

    Reporting 0 would read as "your conversions were fine". The honest answer is
    that the file never contained the evidence.
    """
    summary = foreign_cost_summary(travel_rows(csv_without_fx))
    assert summary["lost_to_poor_conversions"] is None
    assert summary["poor_conversions"] == []
    assert any("not the same as finding nothing wrong" in n for n in summary["notes"])


def test_a_pdf_statement_can_check_conversions(travelled):
    from ledgerlens.enrich.travel import rate_data_availability

    availability = rate_data_availability(travel_rows(travelled))
    assert availability["can_check_conversions"] is True
    assert set(availability["checkable_currencies"]) == {"BRL", "MXN", "INR"}
    assert foreign_cost_summary(travel_rows(travelled))["notes"] == []


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


def test_the_dcc_threshold_catches_a_documented_markup():
    """Published DCC markups start around 3% (interbank plus 2.95% is a cited
    example). A threshold above that would miss the cheapest ones."""
    from ledgerlens.enrich.travel import POOR_RATE_THRESHOLD

    assert POOR_RATE_THRESHOLD < 0.0295
