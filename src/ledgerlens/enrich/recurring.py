"""Recurring-charge detection.

This is the part of LedgerLens that answers the question people actually have:
*what am I still paying for that I have forgotten about?*

The approach is periodicity detection over irregularly-spaced events. For each
merchant, the gaps between consecutive charges are tested against a set of
candidate cadences, with two accommodations for how billing behaves in reality:

* **Drift.** A "monthly" charge lands on 28–31 day gaps, and shifts when a
  billing date falls on a weekend. Tolerances are wide enough to absorb that.
* **Skipped periods.** A failed payment or a paused month leaves a double-length
  gap. A gap is therefore also a match if it is close to a small integer
  multiple of the period, credited at a discount so that a clean series still
  scores higher than a gappy one.

Amount stability is scored separately from timing, because the two fail
independently: a utility bill is perfectly periodic with a different amount
every month, while a subscription that changed price mid-year is stable either
side of one step. Both are still recurring, and both are reported — with
``amount_varies`` and ``price_changed`` flags rather than a lower score.

The payoff is :attr:`Subscription.status`. A charge whose next occurrence is
overdue by more than 1.5 periods is reported as ``likely_cancelled``, which is
what turns a list of charges into "you cancelled this in March" and, more
usefully, "this one is still going".
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# (name, period in days, tolerance in days)
CADENCES: list[tuple[str, float, float]] = [
    ("weekly", 7.0, 2.0),
    ("biweekly", 14.0, 3.5),
    ("monthly", 30.44, 5.0),
    ("quarterly", 91.31, 10.0),
    ("semiannual", 182.62, 15.0),
    ("annual", 365.25, 21.0),
]

MAX_SKIPPED_PERIODS = 3
_AMOUNT_CV_CEILING = 0.35

# A series must account for at least this share of the charges its own cadence
# implies over the window it spans. See the coverage note in ``_analyze``.
_MIN_COVERAGE = 0.7

# A price-point cluster is only meaningful if it is a real part of what the
# merchant charges. A lunch place visited 149 times will always contain seven
# receipts that cost about the same — that is a coincidence, not a plan.
_MIN_CLUSTER_SHARE = 0.25
_MIN_CLUSTER_ABSOLUTE = 6
_MAX_POOL_FOR_ABSOLUTE = 40

# Clusters are *defined* by similar amounts, so their amount-stability score is
# circular and cannot be treated as evidence. Timing has to carry the case on
# its own, so split candidates face a higher regularity bar than whole groups.
_MIN_SPLIT_REGULARITY = 0.8


@dataclass
class Subscription:
    """One detected recurring charge."""

    merchant: str
    cadence: str
    period_days: float
    occurrences: int
    typical_amount_cents: int
    min_amount_cents: int
    max_amount_cents: int
    first_seen: date
    last_seen: date
    expected_next: date
    days_overdue: int
    status: str  # "active" | "overdue" | "likely_cancelled"
    confidence: float
    annualized_cents: int
    amount_varies: bool = False
    price_changed: bool = False
    category: str = "Uncategorized"
    evidence: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "merchant": self.merchant,
            "category": self.category,
            "cadence": self.cadence,
            "typical_amount": round(self.typical_amount_cents / 100, 2),
            "annualized_cost": round(self.annualized_cents / 100, 2),
            "occurrences": self.occurrences,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "expected_next": self.expected_next.isoformat(),
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "amount_varies": self.amount_varies,
            "price_changed": self.price_changed,
        }


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _fit_cadence(gaps: Sequence[float], period: float, tolerance: float) -> float:
    """Score how well ``gaps`` match ``period``, allowing skipped periods."""
    if not gaps:
        return 0.0
    credit = 0.0
    for gap in gaps:
        best = 0.0
        for k in range(1, MAX_SKIPPED_PERIODS + 1):
            # Tolerance widens with sqrt(k): a triple gap has more slack, but
            # not three times as much, or everything matches everything.
            if abs(gap - k * period) <= tolerance * (k**0.5):
                best = max(best, 1.0 if k == 1 else 0.6)
        credit += best
    return credit / len(gaps)


@dataclass
class _AmountStats:
    typical: int
    stability: float
    amount_varies: bool
    price_changed: bool


def _amount_stats(amounts: Sequence[int]) -> _AmountStats:
    """Summarise a series of charge amounts.

    ``stability`` scores how fixed the price is, and deliberately uses the
    *lower* of the overall and recent variation. A subscription that went from
    $10.99 to $11.99 has high overall variation but is perfectly stable now, and
    should not be scored as though the price were random.

    ``amount_varies`` is the opposite question — is this a fixed-price
    subscription or a usage-based bill? — and so looks at the whole series. A
    utility bill that climbs steadily all year varies, even though its last
    three charges sit close together.
    """
    positive = [abs(a) for a in amounts]
    typical = int(statistics.median(positive[-3:] if len(positive) >= 3 else positive))
    mean = statistics.fmean(positive)
    if mean == 0:
        return _AmountStats(typical, 0.0, False, False)

    overall_cv = (statistics.pstdev(positive) / mean) if len(positive) > 1 else 0.0
    recent = positive[-3:]
    recent_mean = statistics.fmean(recent)
    recent_cv = (
        statistics.pstdev(recent) / recent_mean if len(recent) > 1 and recent_mean else 0.0
    )

    effective_cv = min(overall_cv, recent_cv)
    stability = max(0.0, 1.0 - min(1.0, effective_cv / _AMOUNT_CV_CEILING))
    price_changed = bool(positive[0]) and abs(positive[-1] - positive[0]) / positive[0] > 0.05

    return _AmountStats(typical, stability, overall_cv > 0.10, price_changed)


def _analyze(
    merchant: str,
    rows: Sequence[Mapping[str, object]],
    *,
    as_of: date,
    min_occurrences: int,
) -> Subscription | None:
    dates = sorted(_as_date(r["posted_on"]) for r in rows)
    if len(dates) < min_occurrences:
        return None

    gaps = [float((b - a).days) for a, b in zip(dates, dates[1:], strict=False)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None

    scored = [
        (name, period, _fit_cadence(gaps, period, tol)) for name, period, tol in CADENCES
    ]
    cadence, nominal_period, regularity = max(scored, key=lambda s: s[2])
    if regularity < 0.6:
        return None

    # Use the observed median gap rather than the nominal cadence: a "monthly"
    # charge on the 1st has a different real period than one on the 31st.
    median_gap = statistics.median(gaps)
    effective_period = median_gap if median_gap > 0 else nominal_period

    # Coverage guards against the multiple-comparisons problem. Six cadences are
    # tested and the best-fitting one wins, so three scattered charges will
    # always resemble *something* — three purchases eight months apart look
    # "semiannual". A real subscription recurs across the whole window it spans,
    # so compare how many charges there are against how many that cadence
    # implies there should be.
    span_days = (dates[-1] - dates[0]).days
    implied = span_days / effective_period + 1 if effective_period > 0 else len(dates)
    coverage = min(1.0, len(dates) / implied) if implied > 0 else 0.0
    if coverage < _MIN_COVERAGE:
        return None

    ordered = sorted(rows, key=lambda r: _as_date(r["posted_on"]))
    amounts = [int(r["amount_cents"]) for r in ordered]
    stats = _amount_stats(amounts)

    count_factor = min(1.0, (len(dates) - 2) / 4)
    confidence = (
        0.40 * regularity
        + 0.25 * stats.stability
        + 0.20 * coverage
        + 0.15 * count_factor
    )

    last_seen = dates[-1]
    expected_next = last_seen + timedelta(days=round(effective_period))
    days_overdue = (as_of - expected_next).days

    if days_overdue > 1.5 * effective_period:
        status = "likely_cancelled"
    elif days_overdue > 0.5 * effective_period:
        status = "overdue"
    else:
        status = "active"

    annualized = int(round(stats.typical * (365.25 / effective_period)))

    return Subscription(
        merchant=merchant,
        cadence=cadence,
        period_days=round(effective_period, 1),
        occurrences=len(dates),
        typical_amount_cents=stats.typical,
        min_amount_cents=min(abs(a) for a in amounts),
        max_amount_cents=max(abs(a) for a in amounts),
        first_seen=dates[0],
        last_seen=last_seen,
        expected_next=expected_next,
        days_overdue=max(0, days_overdue),
        status=status,
        confidence=min(1.0, confidence),
        annualized_cents=annualized,
        amount_varies=stats.amount_varies,
        price_changed=stats.price_changed,
        category=str(ordered[-1].get("category", "Uncategorized")),
        evidence={
            "regularity": round(regularity, 3),
            "amount_stability": round(stats.stability, 3),
            "median_gap_days": round(median_gap, 1),
        },
    )


def _amount_clusters(
    rows: Sequence[Mapping[str, object]], tolerance: float = 0.02
) -> list[list[Mapping[str, object]]]:
    """Split a merchant's charges into clusters of similar amount.

    A single merchant can bill for several different things — an app store
    charging for three subscriptions, say. When the merchant as a whole shows no
    clean period, its distinct price points often each do.
    """
    clusters: list[list[Mapping[str, object]]] = []
    for row in sorted(rows, key=lambda r: abs(int(r["amount_cents"]))):
        amount = abs(int(row["amount_cents"]))
        placed = False
        for cluster in clusters:
            anchor = abs(int(cluster[0]["amount_cents"]))
            if anchor and abs(amount - anchor) / anchor <= tolerance:
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    return clusters


def _cluster_is_substantial(cluster: Sequence[Mapping[str, object]], pool: int) -> bool:
    """Is this price point a real part of the merchant's activity, or a coincidence?"""
    if len(cluster) >= _MIN_CLUSTER_SHARE * pool:
        return True
    return len(cluster) >= _MIN_CLUSTER_ABSOLUTE and pool <= _MAX_POOL_FOR_ABSOLUTE


def detect_subscriptions(
    transactions: Iterable[Mapping[str, object]],
    *,
    as_of: date | None = None,
    min_occurrences: int = 3,
    min_confidence: float = 0.55,
    include_inflows: bool = False,
) -> list[Subscription]:
    """Find recurring charges among ``transactions``.

    Each mapping needs ``posted_on``, ``amount_cents`` and ``merchant``; an
    optional ``category`` is carried through to the result.

    ``as_of`` anchors the active/cancelled judgement and defaults to today. It
    is a parameter rather than an implicit ``date.today()`` so that the test
    suite is deterministic.
    """
    today = as_of or date.today()

    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in transactions:
        amount = int(row["amount_cents"])
        if not include_inflows and amount >= 0:
            continue  # subscriptions are money leaving the account
        groups.setdefault(str(row["merchant"]), []).append(row)

    found: list[Subscription] = []
    for merchant, rows in groups.items():
        whole = _analyze(merchant, rows, as_of=today, min_occurrences=min_occurrences)
        whole_ok = whole is not None and whole.confidence >= min_confidence

        # Unstable amounts are the signal that a merchant is really several
        # things. An app store billing $2.99 and $10.99 on alternating dates
        # looks like one tidy fortnightly charge until you look at the prices,
        # so a low stability score triggers a split attempt even when the
        # merged series scored well enough to be accepted.
        stability = whole.evidence.get("amount_stability", 1.0) if whole else 0.0
        split: list[Subscription] = []
        if not whole_ok or stability < 0.5:
            clusters = _amount_clusters(rows)
            if len(clusters) >= 2:
                for cluster in clusters:
                    # Without this, a corner shop with two hundred visits yields
                    # a "subscription" for whichever three receipts happened to
                    # cost the same and fall a month apart.
                    if not _cluster_is_substantial(cluster, len(rows)):
                        continue
                    sub = _analyze(
                        merchant, cluster, as_of=today, min_occurrences=min_occurrences
                    )
                    if (
                        sub
                        and sub.confidence >= min_confidence
                        and sub.evidence.get("regularity", 0) >= _MIN_SPLIT_REGULARITY
                    ):
                        split.append(sub)

        if len(split) >= 2:
            found.extend(split)
        elif whole_ok:
            found.append(whole)
        else:
            found.extend(split)

    found.sort(key=lambda s: (-s.annualized_cents, s.merchant))
    return found
