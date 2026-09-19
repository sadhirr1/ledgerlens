"""File-level date-format resolution.

The interesting cases are the ambiguous ones — a per-row parser gets these
wrong silently, which is the failure mode worth testing hardest.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledgerlens.ingest.dates import DateParseError, resolve_date_format


def test_iso_is_unambiguous():
    plan = resolve_date_format(["2026-03-04", "2026-03-19"])
    assert plan.confidence == 1.0
    assert plan.parse("2026-03-04") == date(2026, 3, 4)


def test_single_day_above_twelve_settles_the_whole_file():
    # Only the 25th disambiguates; every other row is ambiguous on its own.
    samples = ["03/04/2026", "05/06/2026", "25/03/2026", "07/08/2026"]
    plan = resolve_date_format(samples)
    assert plan.day_first is True
    assert plan.confidence == 1.0
    # The rows that were ambiguous in isolation now read day-first too.
    assert plan.parse("03/04/2026") == date(2026, 4, 3)


def test_month_first_when_a_month_component_exceeds_twelve():
    samples = ["03/04/2026", "12/25/2026", "01/09/2026"]
    plan = resolve_date_format(samples)
    assert plan.day_first is False
    assert plan.parse("03/04/2026") == date(2026, 3, 4)


def test_genuinely_ambiguous_column_defaults_but_reports_low_confidence():
    samples = ["03/04/2026", "05/06/2026", "07/08/2026", "09/10/2026"]
    plan = resolve_date_format(samples)
    assert plan.confidence <= 0.8
    assert "ambiguous" in plan.reason.lower()


def test_hint_breaks_a_tie():
    samples = ["03/04/2026", "05/06/2026"]
    assert resolve_date_format(samples, day_first_hint=True).day_first is True
    assert resolve_date_format(samples, day_first_hint=False).day_first is False


def test_span_heuristic_prefers_the_contiguous_reading():
    # Day-first keeps these inside two months; month-first scatters them
    # across most of a year.
    samples = ["05/03/2026", "11/03/2026", "19/03/2026", "02/04/2026", "27/04/2026"]
    plan = resolve_date_format(samples)
    assert plan.day_first is True


def test_textual_and_time_bearing_formats():
    assert resolve_date_format(["01-Feb-2026", "14-Mar-2026"]).parse("01-Feb-2026") == date(
        2026, 2, 1
    )
    plan = resolve_date_format(["2026-03-04 14:22", "2026-03-05 09:00"])
    assert plan.parse("2026-03-04 14:22") == date(2026, 3, 4)


def test_unparseable_column_raises():
    with pytest.raises(DateParseError):
        resolve_date_format(["not a date", "also not"])


def test_empty_column_raises():
    with pytest.raises(DateParseError):
        resolve_date_format(["", "  "])


def test_explicit_format_must_actually_parse():
    with pytest.raises(DateParseError):
        resolve_date_format(["03/04/2026"], explicit_format="%Y-%m-%d")
