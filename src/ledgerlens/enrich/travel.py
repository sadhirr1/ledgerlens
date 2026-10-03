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

That one is detectable without any rate lookup, which matters because this
project makes no network calls. Every charge in a given currency implies a rate
— the local amount divided by what you were billed. Across a trip those land
tightly together, because they all went through the same card network within days
of each other. A conversion that was handled by somebody else sits visibly off
that cluster, and the gap between it and the others is what it cost.
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

# A conversion has to be this much worse than the rest to be worth reporting.
# Documented dynamic currency conversion markups start around 3% — a commonly
# cited example is interbank plus 2.95% — and run far higher. A card network's
# own rate moves well under 1% day to day for a major pair. The threshold sits
# just below the bottom of the DCC range so the cheapest ones are still caught;
# the exact percentage is always reported alongside, so a borderline case can be
# judged rather than taken on trust.
POOR_RATE_THRESHOLD = 0.025
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
class PoorConversion:
    """A charge converted at a materially worse rate than its neighbours."""

    merchant: str
    posted_on: date
    currency: str
    rate: float
    typical_rate: float
    billed_cents: int
    extra_cost_cents: int

    def to_dict(self) -> dict[str, object]:
        return {
            "merchant": self.merchant,
            "date": self.posted_on.isoformat(),
            "currency": self.currency,
            "rate_you_got": round(self.rate, 4),
            "typical_rate": round(self.typical_rate, 4),
            "billed": round(self.billed_cents / 100, 2),
            "extra_cost": round(self.extra_cost_cents / 100, 2),
            "worse_by_percent": round(
                (self.typical_rate - self.rate) / self.typical_rate * 100, 1
            ),
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


def find_poor_conversions(
    transactions: Iterable[Mapping[str, object]],
    *,
    threshold: float = POOR_RATE_THRESHOLD,
    min_samples: int = MIN_RATES_FOR_COMPARISON,
) -> list[PoorConversion]:
    """Find charges converted at a materially worse rate than their neighbours.

    No reference rate is fetched. Each currency's own transactions supply the
    benchmark: the median implied rate is what the card network was giving at the
    time, and a charge well below it was converted by somebody else.
    """
    by_currency: dict[str, list[Mapping[str, object]]] = {}
    for row in transactions:
        rate = row.get("fx_rate")
        currency = row.get("original_currency")
        if rate and currency and float(rate) > 0 and not row.get("is_fee"):
            by_currency.setdefault(str(currency), []).append(row)

    found: list[PoorConversion] = []
    for currency, rows in by_currency.items():
        if len(rows) < min_samples:
            continue
        rates = [float(r["fx_rate"]) for r in rows]
        typical = statistics.median(rates)
        if typical <= 0:
            continue

        for row in rows:
            rate = float(row["fx_rate"])
            shortfall = (typical - rate) / typical
            if shortfall <= threshold:
                continue
            billed = abs(int(row["amount_cents"]))
            original = int(row.get("original_amount_cents") or 0)
            if not original:
                continue
            fair_billed = original / typical
            found.append(
                PoorConversion(
                    merchant=str(row.get("merchant", "")) or "Unknown",
                    posted_on=_as_date(row["posted_on"]),
                    currency=currency,
                    rate=rate,
                    typical_rate=typical,
                    billed_cents=billed,
                    extra_cost_cents=int(round(billed - fair_billed)),
                )
            )

    found.sort(key=lambda c: -c.extra_cost_cents)
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
    poor = find_poor_conversions(rows)
    poor_cents = sum(p.extra_cost_cents for p in poor)

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
    if foreign and not availability["can_check_conversions"]:
        if not availability["with_original_amount_and_rate"]:
            notes.append(
                "This export does not include the original amounts or exchange "
                "rates, so conversion quality could not be checked. That is not "
                "the same as finding nothing wrong. PDF statements normally print "
                "both beside each foreign charge; most CSV exports drop them."
            )
        else:
            notes.append(
                f"Only {availability['with_original_amount_and_rate']} foreign "
                f"charges carry a rate, which is too few in any one currency to "
                f"establish what a normal rate looked like "
                f"({MIN_RATES_FOR_COMPARISON} are needed)."
            )

    return {
        "foreign_spend": round(spend_cents / 100, 2),
        "foreign_transaction_fees": round(fee_cents / 100, 2),
        "fee_count": len(fees),
        "effective_fee_percent": (
            round(fee_cents / spend_cents * 100, 2) if spend_cents else 0.0
        ),
        "poor_conversions": [p.to_dict() for p in poor],
        "lost_to_poor_conversions": (
            round(poor_cents / 100, 2) if availability["can_check_conversions"] else None
        ),
        "total_cost_of_going_abroad": round((fee_cents + poor_cents) / 100, 2),
        "by_currency": currencies,
        "rate_data": availability,
        "notes": notes,
    }
