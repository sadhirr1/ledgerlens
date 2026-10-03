"""Trips, and what foreign spending really costs.

Three things are worth knowing about a card used abroad, and none of them can be
answered one transaction at a time.

**Where the money went.** Spending grouped by country rather than by merchant.

**When the trips were.** Foreign charges cluster: a fortnight in Brazil is a
dense run of transactions with nothing either side. Clustering by date gap
recovers the trips without being told about them, which is what turns "you spent
$1,430 in Brazil" into "your 12–22 September trip cost $1,430".

The distinction that matters here is between travelling and shopping. Buying
something from a British website is a foreign transaction and is not a trip. A
trip leaves a different fingerprint — several merchants, over several days — so
an isolated foreign charge, or a run of charges at a single merchant, is not
reported as one. That is deliberately the conservative direction: telling
somebody they went to London because they bought a jumper is worse than missing
a short trip.

**What the conversion cost.** Two separate charges hide inside a foreign
purchase. The issuer's foreign transaction fee, which is itemised and can simply
be summed. And the exchange rate — which is not itemised, and is where dynamic
currency conversion lives: the card machine abroad offers to bill you in your
home currency, you accept, and the merchant's payment processor sets the rate
instead of your card network. It costs a few percent and nothing on the
statement says so.

Detecting it is less obvious than it first appears, and the obvious approach is
wrong. When DCC is accepted the transaction reaches the issuer **already in the
cardholder's own currency**, so the issuer has nothing to convert and prints no
conversion detail at all. Mastercard's merchant guide is explicit: the account is
"debited using the exchange rate offered by the acquirer", and there are "NO
currency conversion details on cardholder statement".

So a DCC'd charge carries no local amount and no rate. Comparing implied rates
between transactions therefore cannot find one — it only ever compares the
charges that were *not* converted at the till.

What identifies DCC is the absence itself, read in context. On a statement where
foreign charges normally print their local amount and rate, one that prints only
a home-currency figure despite an unmistakably foreign merchant is the odd one
out. A foreign transaction fee sitting beside it corroborates this: the issuer
treats a charge as foreign based on where it was processed rather than what
currency it arrived in, so the fee says "this was abroad" while the missing
detail says "somebody else did the conversion".

It cannot be priced. Without the local amount there is nothing to compare a fair
rate against, so the honest output is that the charge looks converted at the
point of sale and that such conversions typically run a few percent above the
card network's rate — not a figure presented as if it were measured.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from ledgerlens.enrich.foreign import ambiguous_location, country_name

# A gap longer than this ends a trip. Long enough to cover a few quiet days,
# short enough to keep two trips to the same country separate.
TRIP_GAP_DAYS = 6

# What separates travelling from buying something from a foreign website.
MIN_TRIP_TRANSACTIONS = 3
MIN_TRIP_MERCHANTS = 2
MIN_TRIP_DAYS = 2

# Before concluding that a missing conversion line is meaningful, the statement
# has to be one that prints conversion lines at all. A few siblings carrying them
# inside the same trip establishes that.
MIN_SIBLINGS_WITH_DETAIL = 3

# A charge is matched to its fee by date and proportion; issuers round, so the
# amount match is loose.
FEE_MATCH_TOLERANCE = 0.25

# And the date match has to be looser still, for two reasons that compound.
#
# A statement's date column is not one thing. A real Amex statement marks some
# rows with an asterisk meaning "posting date" and leaves others as the
# transaction date, in the same column — so two rows for the same purchase can
# legitimately differ by a day or more.
#
# On top of that, a purchase abroad happens at a moment that is already a
# different calendar date at home. Buy something in Mumbai late in the evening
# and it is still the previous afternoon in New York. Whichever date the issuer
# records, it is the issuer's calendar, not the shop's, and the local moment is
# not recoverable from the statement at all.
#
# So a fee is matched within a window rather than on an exact date.
FEE_MATCH_DAYS = 3

# How many rates are needed before a currency's typical rate is worth reporting.
MIN_RATES_FOR_COMPARISON = 4


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


@dataclass
class Trip:
    """A run of foreign transactions that looks like one journey."""

    countries: list[str]
    start: date
    end: date
    transactions: int
    spend_cents: int
    fee_cents: int
    currencies: list[str]
    top_merchants: list[str] = field(default_factory=list)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "countries": [country_name(c) for c in self.countries],
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "transactions": self.transactions,
            "spend": round(self.spend_cents / 100, 2),
            "fees": round(self.fee_cents / 100, 2),
            "total_cost": round((self.spend_cents + self.fee_cents) / 100, 2),
            "currencies": self.currencies,
            "top_merchants": self.top_merchants,
        }


@dataclass
class SuspectedConversion:
    """A charge that looks converted at the till rather than by the card network.

    Deliberately not priced. The local amount is absent — that absence is the
    whole signal — so there is nothing to compute a fair rate against. Reporting
    a number here would be inventing one.
    """

    merchant: str
    posted_on: date
    billed_cents: int
    country: str | None
    fee_observed: bool
    siblings_with_detail: int
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "merchant": self.merchant,
            "date": self.posted_on.isoformat(),
            "billed": round(self.billed_cents / 100, 2),
            "country": country_name(self.country) if self.country else None,
            "foreign_fee_charged": self.fee_observed,
            "confidence": round(self.confidence, 2),
            "why": (
                "billed in your own currency at a foreign merchant, with no "
                "conversion detail, while "
                f"{self.siblings_with_detail} other charges on the same trip "
                "carry theirs"
                + (
                    "; a foreign transaction fee was charged on it, so the issuer "
                    "treated it as a foreign purchase"
                    if self.fee_observed
                    else ""
                )
            ),
            "cost": None,
        }


def _foreign_rows(rows: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [r for r in rows if r.get("country") or r.get("original_currency")]


def unresolved_locations(
    transactions: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    """Count charges whose location code could be a country or a US state.

    ``IN`` is India and Indiana. Without a foreign currency on the row the
    domestic reading wins, which is the right default and also means a whole
    trip can go unreported. Returning the tally lets the caller say so.
    """
    counts: dict[str, int] = {}
    for row in transactions:
        if row.get("country") or row.get("original_currency"):
            continue
        code = ambiguous_location(str(row.get("raw_description") or row.get("merchant") or ""))
        if code:
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def detect_trips(
    transactions: Iterable[Mapping[str, object]],
    *,
    gap_days: int = TRIP_GAP_DAYS,
    min_transactions: int = MIN_TRIP_TRANSACTIONS,
) -> list[Trip]:
    """Group foreign transactions into trips.

    Fees are usually described only as "foreign transaction fee" with no country
    of their own, so they are attributed to whichever trip's dates they fall in
    rather than by location.
    """
    rows = list(transactions)
    foreign = sorted(_foreign_rows(rows), key=lambda r: _as_date(r["posted_on"]))
    if not foreign:
        return []

    clusters: list[list[Mapping[str, object]]] = [[foreign[0]]]
    for row in foreign[1:]:
        gap = (_as_date(row["posted_on"]) - _as_date(clusters[-1][-1]["posted_on"])).days
        if gap > gap_days:
            clusters.append([row])
        else:
            clusters[-1].append(row)

    fees = [r for r in rows if r.get("is_fee")]
    trips: list[Trip] = []

    for cluster in clusters:
        merchants = [str(r.get("merchant", "")) for r in cluster]
        dates = [_as_date(r["posted_on"]) for r in cluster]
        start, end = min(dates), max(dates)

        # Travelling, or just buying from abroad? A trip spreads across days and
        # merchants; a website order does neither.
        if (
            len(cluster) < min_transactions
            or len({m for m in merchants if m}) < MIN_TRIP_MERCHANTS
            or (end - start).days + 1 < MIN_TRIP_DAYS
        ):
            continue

        spend = sum(abs(int(r["amount_cents"])) for r in cluster if int(r["amount_cents"]) < 0)
        fee_total = sum(
            abs(int(f["amount_cents"]))
            for f in fees
            if start <= _as_date(f["posted_on"]) <= end
        )

        by_merchant: dict[str, int] = {}
        for row in cluster:
            amount = int(row["amount_cents"])
            if amount < 0:
                name = str(row.get("merchant", "")) or "Unknown"
                by_merchant[name] = by_merchant.get(name, 0) + abs(amount)

        trips.append(
            Trip(
                countries=sorted({str(r["country"]) for r in cluster if r.get("country")}),
                start=start,
                end=end,
                transactions=len(cluster),
                spend_cents=spend,
                fee_cents=fee_total,
                currencies=sorted(
                    {str(r["original_currency"]) for r in cluster if r.get("original_currency")}
                ),
                top_merchants=[
                    m for m, _ in sorted(by_merchant.items(), key=lambda kv: -kv[1])[:5]
                ],
            )
        )

    trips.sort(key=lambda t: t.start, reverse=True)
    return trips


def _looks_foreign(descriptor: str, trip_countries: Sequence[str]) -> str | None:
    """Does this descriptor name a place abroad?

    An unambiguous country code stands alone. An ambiguous one — ``IN``, ``CA`` —
    counts only when it matches a country the surrounding trip already
    established by other means, which is the same corroboration rule used
    everywhere else, borrowing the trip as the evidence.
    """
    from ledgerlens.enrich.foreign import detect_country

    direct = detect_country(descriptor)
    if direct:
        return direct
    code = ambiguous_location(descriptor)
    if code and code in set(trip_countries):
        return code
    return None


def _match_fee(
    charge: Mapping[str, object],
    fees: Sequence[Mapping[str, object]],
    expected_cents: float,
    claimed: set[int],
) -> int | None:
    """Find the fee row belonging to ``charge``, or None.

    Matched on proportion within a few days rather than on an exact date, because
    a statement's dates are not precise enough to do better: the column mixes
    transaction and posting dates, and a purchase abroad falls on a different
    calendar day at home anyway.

    The closest amount wins, and a fee is only ever claimed by one charge, so two
    similar charges a day apart cannot both point at the same fee.
    """
    charge_date = _as_date(charge["posted_on"])
    best: tuple[float, int] | None = None

    for index, fee in enumerate(fees):
        if index in claimed:
            continue
        gap = abs((_as_date(fee["posted_on"]) - charge_date).days)
        if gap > FEE_MATCH_DAYS:
            continue
        actual = abs(int(fee["amount_cents"]))
        error = abs(actual - expected_cents)
        if error > expected_cents * FEE_MATCH_TOLERANCE:
            continue
        score = (error, gap)
        if best is None or score < (best[0], 0):
            best = (error, index)
    return best[1] if best else None


def find_point_of_sale_conversions(
    transactions: Iterable[Mapping[str, object]],
    trips: Sequence[Trip] | None = None,
) -> list[SuspectedConversion]:
    """Find charges that look converted at the till rather than by the network.

    The signal is a missing conversion line where one would be expected: a
    foreign merchant, a home-currency amount, and sibling charges on the same
    trip that do print their local amount and rate.

    Deliberately scoped to inside a trip. A home-currency charge from a foreign
    online shop looks identical on the statement, and ordering a book from
    abroad is not a conversion decision anyone made at a card terminal.
    """
    rows = list(transactions)
    trips = list(trips) if trips is not None else detect_trips(rows)
    if not trips:
        return []

    fees = [r for r in rows if r.get("is_fee")]
    claimed_fees: set[int] = set()
    found: list[SuspectedConversion] = []

    for trip in trips:
        members = [
            r
            for r in rows
            if not r.get("is_fee")
            and int(r["amount_cents"]) < 0
            and trip.start <= _as_date(r["posted_on"]) <= trip.end
        ]
        with_detail = [r for r in members if r.get("original_currency")]
        if len(with_detail) < MIN_SIBLINGS_WITH_DETAIL:
            # This statement does not print conversion detail anyway, so a
            # missing line says nothing.
            continue

        detailed_spend = sum(abs(int(r["amount_cents"])) for r in with_detail)
        expected_fee_rate = (trip.fee_cents / detailed_spend) if detailed_spend else 0.0

        for row in members:
            if row.get("original_currency"):
                continue
            descriptor = str(row.get("raw_description") or row.get("merchant") or "")
            country = _looks_foreign(descriptor, trip.countries)
            if not country:
                continue

            billed = abs(int(row["amount_cents"]))
            fee_observed = False
            if expected_fee_rate > 0:
                matched = _match_fee(row, fees, billed * expected_fee_rate, claimed_fees)
                if matched is not None:
                    claimed_fees.add(matched)
                    fee_observed = True

            confidence = 0.6 + (0.25 if fee_observed else 0.0)
            if len(with_detail) >= 2 * MIN_SIBLINGS_WITH_DETAIL:
                confidence += 0.1

            found.append(
                SuspectedConversion(
                    merchant=str(row.get("merchant", "")) or "Unknown",
                    posted_on=_as_date(row["posted_on"]),
                    billed_cents=billed,
                    country=country,
                    fee_observed=fee_observed,
                    siblings_with_detail=len(with_detail),
                    confidence=min(1.0, confidence),
                )
            )

    found.sort(key=lambda c: (-c.confidence, -c.billed_cents))
    return found


def rate_data_availability(
    transactions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Report whether the conversion check can run at all.

    This matters more than it looks. PDF statements print the original amount and
    the rate beside each foreign charge; most CSV exports drop both and give only
    the converted figure. Run the conversion check against a CSV and it finds
    nothing — not because the conversions were fine, but because the evidence was
    never in the file.

    Reporting "nothing found" in that situation would be the same class of
    mistake this project tries hardest to avoid: a confident answer that the data
    cannot support. So availability is stated separately from findings.
    """
    rows = list(transactions)
    foreign = [r for r in _foreign_rows(rows) if not r.get("is_fee")]
    with_rates = [r for r in foreign if r.get("fx_rate") and r.get("original_amount_cents")]

    per_currency: dict[str, int] = {}
    for row in with_rates:
        code = str(row.get("original_currency") or "")
        if code:
            per_currency[code] = per_currency.get(code, 0) + 1
    checkable = sorted(c for c, n in per_currency.items() if n >= MIN_RATES_FOR_COMPARISON)

    return {
        "foreign_transactions": len(foreign),
        "with_original_amount_and_rate": len(with_rates),
        "checkable_currencies": checkable,
        "can_check_conversions": bool(checkable),
    }


def foreign_cost_summary(
    transactions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Everything the conversions and fees added up to."""
    rows = list(transactions)
    fees = [r for r in rows if r.get("is_fee")]
    foreign = [r for r in _foreign_rows(rows) if int(r["amount_cents"]) < 0]

    fee_cents = sum(abs(int(f["amount_cents"])) for f in fees)
    spend_cents = sum(abs(int(r["amount_cents"])) for r in foreign)
    suspected = find_point_of_sale_conversions(rows)

    by_currency: dict[str, dict[str, object]] = {}
    for row in foreign:
        code = str(row.get("original_currency") or "")
        if not code:
            continue
        entry = by_currency.setdefault(
            code, {"transactions": 0, "billed_cents": 0, "rates": []}
        )
        entry["transactions"] = int(entry["transactions"]) + 1
        entry["billed_cents"] = int(entry["billed_cents"]) + abs(int(row["amount_cents"]))
        if row.get("fx_rate"):
            entry["rates"].append(float(row["fx_rate"]))  # type: ignore[union-attr]

    currencies = {
        code: {
            "transactions": entry["transactions"],
            "billed": round(int(entry["billed_cents"]) / 100, 2),
            "typical_rate": (
                round(statistics.median(entry["rates"]), 4) if entry["rates"] else None  # type: ignore[arg-type]
            ),
        }
        for code, entry in sorted(by_currency.items())
    }

    availability = rate_data_availability(rows)
    notes: list[str] = []
    if foreign and not availability["with_original_amount_and_rate"]:
        notes.append(
            "This export prints no original amounts or exchange rates, so "
            "point-of-sale conversions cannot be spotted. That is not the same "
            "as finding none: the signal is a charge missing its conversion "
            "line while others have theirs, and here nothing has one. PDF "
            "statements normally print them; most CSV exports drop them."
        )
    if suspected:
        notes.append(
            "Suspected point-of-sale conversions cannot be priced. The local "
            "amount is absent — that absence is the signal — so there is nothing "
            "to compare a fair rate against. Such conversions typically run a few "
            "percent above the card network's rate."
        )

    return {
        "foreign_spend": round(spend_cents / 100, 2),
        "foreign_transaction_fees": round(fee_cents / 100, 2),
        "fee_count": len(fees),
        "effective_fee_percent": (
            round(fee_cents / spend_cents * 100, 2) if spend_cents else 0.0
        ),
        "suspected_point_of_sale_conversions": [c.to_dict() for c in suspected],
        "suspected_conversion_count": len(suspected),
        "cost_of_suspected_conversions": None,
        "total_measurable_cost_of_going_abroad": round(fee_cents / 100, 2),
        "by_currency": currencies,
        "rate_data": availability,
        "notes": notes,
    }
