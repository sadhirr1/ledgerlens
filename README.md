# LedgerLens

**Ask an AI about your actual spending — without your bank statements leaving your machine.**

[![CI](https://github.com/sadhirr/ledgerlens/actions/workflows/ci.yml/badge.svg)](https://github.com/sadhirr/ledgerlens/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](https://modelcontextprotocol.io)

LedgerLens is a [Model Context Protocol](https://modelcontextprotocol.io) server that
points at a folder of bank and credit card CSV exports and makes them queryable in
plain language. No API keys, no bank logins, no third-party service — it reads the
files you already have and keeps everything in a local SQLite database.

```
You:    What subscriptions am I paying for?

Claude: You have 4 active recurring charges costing $1,227/year:

          Amazon Web Services   monthly    $68.30    $804.73/yr   (amount varies)
          Netflix               monthly    $15.99    $194.68/yr
          Spotify               monthly    $11.99    $143.59/yr   (price rose in Oct)
          GitHub                annual     $84.00     $83.94/yr

        One looks cancelled: Planet Fitness, $24.99/month. Last charged
        2 Jan 2026 and nothing since — you're no longer paying for it.
```

---

## Why this exists

Every personal-finance tool wants your bank login. LedgerLens doesn't, because it
doesn't need one: your bank already gives you the data as a CSV download. The hard
part was never getting the data — it's that every bank exports something different,
and none of it is in a shape you can ask questions about.

That's what this is: the boring, careful layer between "a folder of messy CSVs" and
"a question about your money."

## Quickstart

```bash
pip install ledgerlens          # or: uv tool install ledgerlens
```

Download your statements as CSV from your bank into a folder, then:

```bash
ledgerlens import ~/Statements
ledgerlens subscriptions
```

To use it from an MCP client, add it to your config — for Claude Desktop, in
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ledgerlens": {
      "command": "ledgerlens",
      "args": ["serve"]
    }
  }
}
```

Then ask things like *"what did I spend on food last month?"*, *"how does this
quarter compare to last?"*, or *"what am I paying for that I don't use?"*

## What's actually hard about this

Most of the interesting engineering is in the gap between what a bank exports and
what you can compute on. Some of the problems worth knowing about:

### Dates are ambiguous, and getting it wrong is silent

`03/04/2026` is March 4th in a US export and April 3rd in a European one. Deciding
per-row is how a statement quietly ends up with transactions scattered across the
wrong months — no error, just wrong answers forever after.

LedgerLens resolves the format **once per file**, using the whole column as evidence.
A single `25/03/2026` anywhere in the file settles the entire file. If every row is
ambiguous, it falls back to whichever reading produces a contiguous date range — a
statement covers a period, not scattered days — and reports low confidence rather
than guessing silently:

```
ambiguous_dates.csv   4 imported, 0 skipped
  ! date format: %m/%d/%Y (confidence 0.50): genuinely ambiguous (no component
    exceeds 12); defaulting to month-first. Set day_first in ledgerlens.yaml
```

### Direction is a convention, not a fact

Three conventions appear in real exports: a signed `Amount` column; separate `Debit`
and `Credit` columns; or unsigned amounts plus a `DR`/`CR` type column. Read it wrong
and every number is inverted while looking perfectly plausible.

It's inferred from the shape of the data, and when the file is genuinely
under-determined — unsigned amounts, no type column — that assumption is reported
rather than buried:

```
amex_preamble.csv   193 imported, 0 skipped
  ! amount direction: assume_outflow (confidence 0.55): amounts are unsigned
    with no type column; assuming a card-style spend-only export
```

There's also a `Balance` column sitting next to the amount in most exports, and it's
numeric, and picking it would invert every total in the database. It's excluded
explicitly, and there's [a test](tests/test_dialect.py) whose only job is to keep it
that way.

### Merchant names are machine output

```
SQ *BLUE BOTTLE #4412 OAKLAND CA 05/14
TST* SWEETGREEN 0184 NEW YORK NY
AMZN Mktp US*2H4XY9DK3 AMZN.COM/BILL WA
```

Group by the raw string and one coffee shop appears forty times. Normalization
strips processor prefixes, store numbers, reference codes and trailing locations —
the multi-word-city case (`SAN JOSE CA`) being the one that naive trailing-token
removal gets wrong, leaving a stray `SAN` behind. The raw descriptor is always kept
alongside; this step is lossy by design and source data is never destroyed.

### Finding subscriptions is periodicity detection, not pattern matching

The headline feature is "what am I still paying for?", which means finding periodic
events in an irregularly-spaced series. Two things make it non-trivial:

- **Drift.** Monthly charges land on 28–31 day gaps and shift when a billing date
  hits a weekend. Tolerances absorb that.
- **Skipped periods.** A failed payment leaves a double-length gap. A gap also counts
  as a match if it's near a small integer multiple of the period, credited at a
  discount so a clean series still outranks a gappy one.

Timing and amount are scored **separately**, because they fail independently: a
utility bill is perfectly periodic with a different amount every month, and a
subscription that changed price is rock-stable either side of one step. Both are
still recurring, and both are reported — flagged `amount_varies` or `price_changed`
rather than scored down.

The payoff is `status`. A charge overdue by more than 1.5 periods is reported as
`likely_cancelled`, which is what turns a list into *"you cancelled this in March,
but this other one is still running."*

**Precision matters more than recall here.** Six cadences are tested and the
best-fitting one wins, so given a few hundred transactions, *some* subset will always
look periodic — three purchases eight months apart look "semiannual." A tool that
tells you that you subscribe to your corner shop is worse than one that misses
something. Two guards handle it: a **coverage** requirement (a real subscription
recurs across the whole window it spans, so the charge count is checked against what
its own cadence implies) and a **substantiality** requirement on price-point
clustering (seven similar receipts out of 149 visits to a lunch place is a
coincidence, not a plan). There's [a test](tests/test_recurring.py) asserting zero
false positives across a fixture containing twelve merchants of ordinary spending.

### Tool results go into a context window, not onto a screen

The budget here is tokens, so two rules are enforced in the query layer rather than
left to each tool: **aggregate by default, detail on request**, and **truncate
loudly**. Every capped result carries `truncated`, the rows returned and the true
total, so the model says *"showing 50 of 812"* instead of confidently summarising a
slice it thinks is everything. `get_transactions` is the only line-item tool and it
refuses to return more than 200 rows — a tool that *can* flood a context window
eventually will.

### Re-importing must be safe

People re-download overlapping ranges constantly: January–March, then February–April.
A naive importer doubles February and inflates your spending by exactly the overlap.

Two mechanisms: a content-hash import log short-circuits an identical file, and every
transaction gets a deterministic content-hash id so the same charge arriving from a
*different* file still collapses to one row. An occurrence counter in that hash keeps
two genuinely identical charges on one day — two $4.50 coffees — from being mistaken
for duplicates of each other. Statement filenames are stripped of period markers, so
`chase_checking_2026q1.csv` and `chase_checking_2026q2.csv` resolve to one account
instead of two.

## MCP tools

| Tool | What it does |
|---|---|
| `import_statements` | Import a folder or file. Auto-detects format; safe to re-run |
| `overview` | Date range, totals, accounts — the natural first call |
| `spending_by_category` | Category totals for a period |
| `spending_by_merchant` | Merchant totals, optionally within a category |
| `monthly_summary` | In, out and net per month |
| `find_subscriptions` | Recurring charges with annual cost and active/cancelled status |
| `get_transactions` | Line-item drill-down (capped, always reports truncation) |
| `compare_periods` | Category-level diff between two windows, biggest movers first |
| `suggest_category_rules` | Uncategorized merchants, ranked by spend |
| `apply_category_rules` | Re-categorize existing transactions after editing rules |

## Privacy

**The runtime makes zero network calls.** Not "minimal", not "anonymized" — none.
[`tests/test_no_network.py`](tests/test_no_network.py) disables socket construction
entirely and runs the whole pipeline: import, enrichment, every query, subscription
detection. Any outbound connection, in any dependency, fails there rather than
quietly in your terminal.

Everything else that follows from that:

- Your CSVs are **read-only**. LedgerLens never writes to your source files.
- Data lives in one local SQLite file under `$XDG_DATA_HOME/ledgerlens/`.
- Account numbers are masked to last-4 on output.
- Categorization is **rule-based, not model-based** — deterministic, offline, and
  the same statement gives the same totals every time.
- No real financial data is in this repository, and a
  [pre-commit hook](scripts/check_no_real_data.py) inspects file *contents* to keep
  it that way. CI enforces it too.

The one honest caveat, stated plainly: when you ask your AI client a question, the
*answer* enters that client's context. LedgerLens never transmits your data anywhere,
but it is a tool for showing your finances to an AI — that's the point of it.

## Configuration

Auto-detection handles most exports. For a bank strange enough to defeat it, drop a
`ledgerlens.yaml` beside the CSVs — any field you omit is still auto-detected:

```yaml
account_name: Barclays Current
date_column: "Transaction Date"
description_column: "Narrative"
day_first: true          # resolve ambiguous dates as DD/MM
negative_is_outflow: true
currency: GBP
skip_rows: 3             # junk above the header
```

Categories come from a [shipped rule pack](src/ledgerlens/rules/default_categories.yaml).
Your own rules go in `$XDG_DATA_HOME/ledgerlens/categories.yaml` and are evaluated
first, so an upgrade never clobbers them:

```yaml
categories:
  - category: Coffee
    match:
      - "blue bottle|verve|four barrel"
```

Then `ledgerlens categories --apply` to update existing transactions.

## Development

```bash
git clone https://github.com/sadhirr/ledgerlens
cd ledgerlens
pip install -e ".[dev]"
pytest
```

The fixtures are generated by a seeded script
([`tests/fixtures/generate.py`](tests/fixtures/generate.py)) and committed, so a fresh
clone tests without running it. They deliberately include every awkward case —
European decimal commas, `windows-1252` bytes, preamble above the header, unsigned
amounts with a DR/CR column, parenthesised negatives, genuinely ambiguous dates — and
**subscriptions planted with known cadence, amount and end date**, so the detector's
tests assert against ground truth rather than against whatever it happened to output
when the test was written. CI checks the generator and the fixtures haven't drifted
apart.

## Contributing

The easiest useful contribution is merchant and category rules: the
[rule pack](src/ledgerlens/rules/default_categories.yaml) and the alias table in
[`merchants.py`](src/ledgerlens/enrich/merchants.py) are both plain lists, and every
bank descriptor you add makes the output better for someone else. Non-US banks are
especially welcome — the header synonyms currently cover English, German, French and
Spanish, and there are a lot more banks than that.

## License

MIT
