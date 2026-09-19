"""Proof that LedgerLens never phones home.

"Your data stays local" is the central promise of this project, and a promise
in a README is worth very little. This test disables socket construction
entirely and then exercises the whole pipeline — import, enrichment, every
query, subscription detection. Any outbound connection, in any dependency,
fails loudly here rather than quietly in a user's terminal.
"""

from __future__ import annotations

import socket

import pytest

from ledgerlens import query
from ledgerlens.enrich.recurring import detect_subscriptions
from ledgerlens.ingest import import_folder, recategorize


class NetworkAccessAttempted(AssertionError):
    """Raised if anything tries to open a socket during the pipeline."""


@pytest.fixture()
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "LedgerLens attempted a network connection; it must run fully offline"
        )

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    return blocked


def test_the_entire_pipeline_runs_with_sockets_disabled(no_network, conn, fixtures):
    results = import_folder(conn, fixtures)
    assert sum(r.rows_inserted for r in results) > 0

    recategorize(conn)

    assert query.overview(conn)["transaction_count"] > 0
    query.spending_by_category(conn)
    query.spending_by_merchant(conn)
    query.monthly_summary(conn)
    query.list_transactions(conn)
    query.top_uncategorized(conn)
    query.compare_periods(
        conn, period_a=("2025-01-01", "2025-06-30"), period_b=("2026-01-01", "2026-06-30")
    )

    subs = detect_subscriptions(query.all_transactions_for_recurrence(conn))
    assert isinstance(subs, list)


def test_the_guard_itself_works(no_network):
    """A test that cannot fail proves nothing; check the block is real."""
    with pytest.raises(NetworkAccessAttempted):
        socket.socket()
