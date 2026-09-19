"""LedgerLens — a local-first MCP server for personal statement data.

Nothing in this package makes a network call. See ``tests/test_no_network.py``,
which enforces that at test time by disabling socket construction entirely.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
