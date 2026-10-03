# LedgerLens

**Ask an AI about your actual spending — without your bank statements leaving your machine.**

[![CI](https://github.com/sadhirr1/ledgerlens/actions/workflows/ci.yml/badge.svg)](https://github.com/sadhirr1/ledgerlens/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-8A2BE2.svg)](https://modelcontextprotocol.io)

LedgerLens is a [Model Context Protocol](https://modelcontextprotocol.io) server that
points at a folder of bank and credit card statements — **CSV or PDF** — and makes
them queryable in plain language. No API keys, no bank logins, no third-party
service. It reads the files you already have and keeps everything in a local
SQLite database.

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

> **[Decisions](DECISIONS.md)** — who this is for, the alternatives I rejected,
> and what each design choice cost. Read that if you care more about the
> reasoning than the code.

---

## Why this exists

Every personal-finance tool wants your bank login. LedgerLens doesn't, because it
doesn't need one: your bank already gives you the data as a CSV download. The hard
part was never getting the data — it's that every bank exports something different,
and none of it is in a shape you can ask questions about.

A PDF statement in particular *looks* like a table and isn't one. The file records
"draw the text `$17.03` at x=531, y=138"; the columns you see are an artifact of
where the ink landed. And even a perfectly structured statement can only ever
describe one month, while every question worth asking — what do my subscriptions
cost a year, am I spending more than last quarter, what am I still paying for —
lives *across* statements.

That's what this is: the boring, careful layer between "a folder of messy CSVs" and
"a question about your money."

## Quickstart

```bash
pip install ledgerlens          # or: uv tool install ledgerlens
```

Download your statements into a folder — CSV if your bank offers it, PDF if it
doesn't — then:

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

Then ask things like *"what did I spend on food last month?"*, *"what did my Brazil
trip cost?"*, *"how much went on foreign transaction fees?"*, or *"what am I
paying for that I don't use?"*

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

### A PDF is a picture of a table, not a table

CSV is the reliable path, but plenty of issuers only offer PDFs, so those are
supported too — and they are considerably harder, because the table has to be
reconstructed from text positions.

**Telling transactions from everything else.** A statement is full of numbers that
aren't transactions: a summary block totalling the section below it, a fees line, an
APR table, the closing date in the page header. Requiring that a row *begin* with a
date and carry an amount in the right-hand money column removes nearly all of them —
`Total Payments and Credits -$340.68` has no date, and
`Purchases 07/31/2023 28.49% (v) $0.00` has one but not in first position.

**Credit cards invert the sign convention.** On a card, a purchase prints as
*positive* and your monthly payment as *negative* — the opposite of a current
account. Read one as the other and you record paying your bill as spending, and
your actual spending as income. The truth is stated outright in the section
headings ("New Charges", "Payments and Credits"), so the extractor tracks section
state as it walks the document and normalizes the sign itself rather than leaving a
later stage to guess from the numbers.

Getting this subtly wrong is easy: the printed sign *restates* the heading's
direction rather than modifying it, so applying both inverts everything. Each
section's majority printed sign is taken as its normal, and only minority rows — a
refund sitting among the charges — flip.

**Statements are laid out Section → Summary → Detail**, and the detail resumes on
later pages under "Detail Continued". Skipping the summary is right; forgetting to
stop skipping silently drops most of the statement.

**The extraction checks its own work.** Reading a PDF is a reconstruction, and its
failures are silent — a dropped page, a row absorbed into the one above, an
inverted sign. But the statement already states what its sections add up to, so
LedgerLens sums what it extracted and compares:

```
amex_2026-09.pdf   35 imported, 0 skipped
  ✓ reconciles against the statement's own printed totals
    (in statement 340.68, extracted 340.68; out statement 587.10, extracted 587.10)
```

When those figures match, the result isn't merely plausible — it's arithmetically
consistent with what the issuer printed. When they don't, you get a warning saying
so and by how much, rather than a confident wrong answer. Scans with no text layer
are rejected with a pointer to OCR; password-protected files say so and tell you
where to put the password.

### A card used abroad means something different

Travel breaks the assumptions a domestic statement is read under. The amount you
are billed is a *conversion* of what the merchant actually charged, with a fee
added, at a rate nothing on the statement names.

**Two-letter country codes collide with US state codes.** `IN` is India and
Indiana. `CA` is Canada and California. `DE` is Delaware and Germany; `PA`,
Pennsylvania and Panama. A lookup table reads `INDIANAPOLIS IN` as a trip to
India.

So the codes are split in two. Codes that aren't US states resolve outright.
Ambiguous ones resolve to a country only with independent evidence — a currency
that isn't the account's. Without it the domestic reading wins, because for most
people most charges are domestic. Currency settles it either way: a charge in BRL
happened in Brazil, whatever the descriptor says.

**Travelling and shopping look identical one row at a time.** Buying from a
British website is a foreign transaction and is not a trip. A trip has a
different fingerprint — several merchants, over several days — so an isolated
foreign charge is not reported as one. Telling someone they went to London
because they bought a jumper is worse than missing a short trip.

**The expensive part is invisible.** Beyond the itemised foreign transaction fee
sits dynamic currency conversion: the card machine abroad offers to bill you in
your home currency, you accept, and the merchant's processor sets the rate
instead of your card network. It costs several percent, and nothing on the
statement says it happened.

It is detectable without looking up a single rate, which matters because this
project makes no network calls. Every foreign charge implies a rate — local
amount over billed amount — and across a trip those cluster tightly, since they
all went through the same network within days of each other. A conversion handled
by somebody else sits visibly off that cluster:

```
Loja Turistica   2026-09-19   rate 4.7500 vs 5.1401 typical
                              7.6% worse - it cost $6.39
```

The benchmark is the statement's own other transactions. No reference data, no
network, and the evidence was in the file all along.

**But only if the file has it.** PDF statements print the original amount and the
rate beside each foreign charge. Most CSV exports drop both and give only the
converted figure. So the conversion check runs against a PDF and cannot run
against a typical CSV — and saying "no bad conversions found" in the second case
would be a confident answer the data does not support:

```
travel_card.csv   14 foreign charges, 0 with rates
  lost to poor conversions: unknown
  ! This export does not include the original amounts or exchange rates, so
    conversion quality could not be checked. That is not the same as finding
    nothing wrong.
```

The same gap costs a whole trip. Country is read off the descriptor, and a code
that is also a US state needs a currency to corroborate it — which a plain CSV
does not have. India silently disappears while Brazil and Mexico survive, since
`BR` and `MX` are not state codes. So the unresolved codes are counted and
reported, with the fix:

```
! 12 charges end in a location code that is both a country and a US state
  (IN x7, CA x5) and carried no currency to settle it, so they were read as
  domestic. If a trip is missing, importing the PDF statement rather than a
  CSV usually resolves it.
```

Elsewhere in this README, CSV is the reliable format and PDF the fallback. For
travel it is the other way round, and that is worth knowing before you conclude
your conversions were fine.

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
| `spending_by_country` | Spending grouped by where each charge happened |
| `find_trips` | Foreign charges clustered into trips, with what each cost |
| `foreign_transaction_costs` | FX fees, and conversions done at a worse rate than the rest |
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
pdf_password: hunter2    # for a locked PDF statement
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
git clone https://github.com/sadhirr1/ledgerlens
cd ledgerlens
pip install -e ".[dev]"
pytest
```

The server works with both generations of the MCP SDK — 2.0 renamed `FastMCP`
to `MCPServer` and little else, so one alias covers it, and a CI job pins the
older SDK to keep that path exercised.

The fixtures are generated by a seeded script
([`tests/fixtures/generate.py`](tests/fixtures/generate.py)) and committed, so a fresh
clone tests without running it. They deliberately include every awkward case —
European decimal commas, `windows-1252` bytes, preamble above the header, unsigned
amounts with a DR/CR column, parenthesised negatives, genuinely ambiguous dates — and
**subscriptions planted with known cadence, amount and end date**, so the detector's
tests assert against ground truth rather than against whatever it happened to output
when the test was written.

The PDF fixtures are generated too, and reproduce the structures that matter: a
card statement with the summary/detail split, inverted signs, detail continuing
across a page break, metadata lines under each charge, a year-to-date block and an
APR table; a bank statement drawn as a ruled grid with a running-balance column; and
a page with no text layer at all. CI checks the generator and the fixtures haven't drifted
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
