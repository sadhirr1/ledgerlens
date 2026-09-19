"""File-level date-format resolution.

``03/04/2026`` is March 4th in a US export and April 3rd in a European one, and
nothing in the row itself tells you which. Deciding per-row is how a statement
silently ends up with transactions scattered across the wrong months.

LedgerLens resolves the format **once per file**, using the whole column as
evidence:

1. Keep only the candidate formats under which *every* sampled value parses.
2. If day-first and month-first candidates both survive, look for a value with
   a component above 12 — a single ``25/03/2026`` settles the entire file.
3. If the column is still genuinely ambiguous (every value has both components
   ≤ 12), fall back to the interpretation that produces the tighter date span,
   since a statement covers a contiguous period rather than scattered dates.
4. Failing that, take the user's hint, then the US default — and report low
   confidence so the import log shows the guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Ordered by specificity. ISO first: it is never ambiguous.
_CANDIDATES: list[tuple[str, bool | None]] = [
    ("%Y-%m-%d", None),
    ("%Y/%m/%d", None),
    ("%Y.%m.%d", None),
    ("%d-%b-%Y", True),
    ("%d %b %Y", True),
    ("%b %d, %Y", False),
    ("%b %d %Y", False),
    ("%d-%B-%Y", True),
    ("%B %d, %Y", False),
    ("%m/%d/%Y", False),
    ("%d/%m/%Y", True),
    ("%m-%d-%Y", False),
    ("%d-%m-%Y", True),
    ("%m.%d.%Y", False),
    ("%d.%m.%Y", True),
    ("%m/%d/%y", False),
    ("%d/%m/%y", True),
    ("%d.%m.%y", True),
    ("%m.%d.%y", False),
]

_TIME_SUFFIX = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\s*[AaPp][Mm])?\s*$")
_NUMERIC_TRIPLE = re.compile(r"^\s*(\d{1,4})[-/.](\d{1,2})[-/.](\d{2,4})\s*$")


class DateParseError(ValueError):
    """Raised when a date column cannot be resolved to a single format."""


@dataclass
class DatePlan:
    """The resolved date format for one file."""

    fmt: str
    day_first: bool | None
    confidence: float
    reason: str

    def parse(self, raw: str) -> date:
        return datetime.strptime(_strip_time(raw), self.fmt).date()

    def describe(self) -> str:
        return f"{self.fmt} (confidence {self.confidence:.2f}): {self.reason}"


def _strip_time(raw: str) -> str:
    return _TIME_SUFFIX.sub("", str(raw).strip()).strip()


def _parses_all(fmt: str, samples: list[str]) -> list[date] | None:
    out: list[date] = []
    for s in samples:
        try:
            out.append(datetime.strptime(_strip_time(s), fmt).date())
        except (ValueError, TypeError):
            return None
    return out


def _has_disambiguating_value(samples: list[str]) -> str | None:
    """Return the first sample whose first or second component exceeds 12."""
    for s in samples:
        m = _NUMERIC_TRIPLE.match(_strip_time(s))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if len(m.group(1)) == 4:  # year-first, unambiguous already
            continue
        if a > 12 or b > 12:
            return s
    return None


def _span_days(dates: list[date]) -> int:
    return (max(dates) - min(dates)).days if dates else 0


def resolve_date_format(
    samples: list[str],
    *,
    day_first_hint: bool | None = None,
    explicit_format: str | None = None,
) -> DatePlan:
    """Resolve the date format for a column, given its values."""
    usable = [s for s in samples if s and str(s).strip()]
    if not usable:
        raise DateParseError("date column is empty")

    if explicit_format:
        if _parses_all(explicit_format, usable) is None:
            raise DateParseError(f"supplied date_format {explicit_format!r} does not parse every row")
        return DatePlan(explicit_format, day_first_hint, 1.0, "format supplied by user override")

    viable: list[tuple[str, bool | None, list[date]]] = []
    for fmt, day_first in _CANDIDATES:
        parsed = _parses_all(fmt, usable)
        if parsed is not None:
            viable.append((fmt, day_first, parsed))

    if not viable:
        raise DateParseError(
            f"no known date format parses every value; first sample: {usable[0]!r}"
        )

    unambiguous = [v for v in viable if v[1] is None]
    if unambiguous:
        fmt, day_first, _ = unambiguous[0]
        return DatePlan(fmt, day_first, 1.0, "ISO-style dates are unambiguous")

    day_first_opts = [v for v in viable if v[1] is True]
    month_first_opts = [v for v in viable if v[1] is False]

    if day_first_opts and not month_first_opts:
        fmt, _, _ = day_first_opts[0]
        witness = _has_disambiguating_value(usable)
        reason = (
            f"only a day-first reading parses every row (e.g. {witness!r})"
            if witness
            else "only a day-first reading parses every row"
        )
        return DatePlan(fmt, True, 1.0, reason)

    if month_first_opts and not day_first_opts:
        fmt, _, _ = month_first_opts[0]
        witness = _has_disambiguating_value(usable)
        reason = (
            f"only a month-first reading parses every row (e.g. {witness!r})"
            if witness
            else "only a month-first reading parses every row"
        )
        return DatePlan(fmt, False, 1.0, reason)

    # Both readings parse. Prefer the tighter span: statements are contiguous.
    df_fmt, _, df_dates = day_first_opts[0]
    mf_fmt, _, mf_dates = month_first_opts[0]
    df_span, mf_span = _span_days(df_dates), _span_days(mf_dates)

    if len(usable) >= 4 and abs(df_span - mf_span) > 45:
        if df_span < mf_span:
            return DatePlan(
                df_fmt, True, 0.8,
                f"ambiguous, but a day-first reading spans {df_span}d vs {mf_span}d",
            )
        return DatePlan(
            mf_fmt, False, 0.8,
            f"ambiguous, but a month-first reading spans {mf_span}d vs {df_span}d",
        )

    if day_first_hint is True:
        return DatePlan(df_fmt, True, 0.7, "ambiguous column; using day_first hint")
    if day_first_hint is False:
        return DatePlan(mf_fmt, False, 0.7, "ambiguous column; using month_first hint")

    return DatePlan(
        mf_fmt, False, 0.5,
        "genuinely ambiguous (no component exceeds 12); defaulting to month-first. "
        "Set day_first in ledgerlens.yaml to override",
    )
