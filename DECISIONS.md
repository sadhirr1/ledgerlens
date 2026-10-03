# Decisions

Notes on why LedgerLens is shaped the way it is. Mostly this is a record of
what I chose *not* to build, and what each of those choices cost.

---

## The problem

Everyone has the data. Your bank hands you a statement every month, and most
people open it once and never again.

The reason isn't laziness. It's that a statement can only ever answer questions
about one month, and every question actually worth asking lives *across* months:

- What do my subscriptions add up to over a year?
- Am I spending more on food than I was six months ago?
- What am I still paying for that I stopped using?

To answer the last one you have to notice a charge appearing in twelve
consecutive statements and then vanishing from the next six. That's eighteen
documents and a cross-reference. No single statement can do it, however well
formatted — and a PDF statement isn't even well formatted for a machine, because
it stores where text was drawn rather than what the columns mean.

So the gap isn't presentation. It's that nobody turns a pile of monthly documents
into one continuous history you can compute over.

## Who this is for

Someone who satisfies **both** of these:

1. They already download their statements, or would be willing to.
2. They have an objection to handing their bank login to a third party.

That's deliberately narrow. Drop either condition and a commercial app is the
better answer, and I'd rather say so than pretend otherwise.

**Who it isn't for:**

- Anyone who wants this to be effortless. It is not. You download files and run
  a command. A tool with a live bank connection updates itself; this does not.
- Anyone who wants something *done* about their spending. This tool reports. It
  cannot cancel a subscription or move money, by design.
- Anyone who needs their partner or accountant to see the same view. Everything
  is local to one machine, and that is the whole point, so sharing is a problem
  it does not attempt to solve.

## Alternatives I considered

**Rocket Money and similar (Plaid-backed apps).** These are good, and honestly
better than this for most people: always current, no manual step, and they will
cancel subscriptions and haggle bills on your behalf. Rejected because they
require a bank login and your transaction history lives on their servers.
That's a reasonable trade for most people and an unacceptable one for some — and
the latter group currently has nothing decent.

**A spreadsheet.** Genuinely viable, and free. Rejected because the work doesn't
compress: every new statement means another round of pasting, cleaning merchant
names by hand, and redoing formulas. It also can't answer a question you didn't
plan a column for.

**Building against a bank API directly (Plaid, TrueLayer, Open Banking).** The
clean engineering answer — real structured data, no parsing. Rejected on three
grounds: it reintroduces the credential problem I was trying to avoid; it costs
money per connection at any scale; and it only covers supported institutions,
which is a poor fit for anyone whose bank isn't on the list.

**Doing nothing.** The honest baseline, and the one most people pick. The bar
this has to clear isn't "better than Rocket Money" — it's "worth the five
minutes of downloading files." That framing drove more decisions than anything
else below.

---

## Decisions, and what they cost

### 1. No network access at all

**Decision.** The tool makes zero outbound connections. Statements are read from
disk, results are written to a local SQLite file.

**Why.** It's the only version of "your data stays private" that is actually
checkable rather than promised. A test disables socket construction and runs the
whole pipeline, so a dependency that started phoning home would fail the build.

**Cost.** No automatic updates, so the data is only as fresh as your last
download. And the honest caveat that belongs in the README rather than buried
here: when you ask an AI client a question, the answer enters that client's
context. The tool never transmits anything, but it is a tool for showing your
finances to an AI, and pretending otherwise would be dishonest.

### 2. CSV first, PDF as a fallback

**Decision.** CSV is the supported path. PDF works, and is treated as a
best-effort fallback.

**Why.** I built CSV-only at first, then went to download my own Amex statements
and found only PDF on offer. A tool that can't read the format people actually
have isn't much use, however clean its internals.

**Cost.** PDF parsing is a reconstruction and will never be as reliable as
reading a CSV. Rather than hide that, every PDF import cross-checks its own
output against the statement's printed section totals and reports whether they
agree. On the statement I built against, both directions reconcile to the cent.
When they don't agree, you get told by how much — a warning being far better
than a confident wrong number.

### 3. Rule-based categorisation, not a language model

**Decision.** Categories come from regex rules in a YAML file. No model is
involved in deciding what a transaction is.

**Why.** Two reasons. Determinism: the same statement categorised twice gives the
same totals, and a spending figure that drifts between runs is worse than no
figure. And it keeps decision 1 intact — a model call would mean a network call.

**Cost.** Coverage. On my own statement, 12 of 22 merchants were categorised; the
rest were independent local places no shipped rule pack could know about. I
accepted that and built the loop instead: a tool that hands the AI your
uncategorised merchants ranked by spend, so it can propose rules you commit to
your own file. The AI is good at *suggesting* rules and bad at being a reliable
arithmetic substrate, so that's the division of labour.

**How I'd know I was wrong.** If most people never write a single rule and just
live with "Uncategorized", the loop failed and the trade was bad.

### 4. Precision over recall on subscription detection

**Decision.** When in doubt, don't report something as a subscription.

**Why.** This is the feature the whole thing exists for, and its failure modes
are not symmetric. Missing one subscription is a small loss. Telling someone they
have a monthly subscription to their corner shop destroys trust in every other
number on the screen.

**Cost.** Real subscriptions get missed — specifically, ones buried among heavy
spending at the same merchant, like a Prime membership inside hundreds of Amazon
orders. I decided that was the right way to fail.

This was not theoretical. The first working version reported fifteen
subscriptions on test data, of which ten were nonsense — Target, Shell, a lunch
place. With six candidate cadences and a few hundred transactions, *something*
always looks periodic. Two additional constraints fixed it: a real subscription
has to recur across the whole window it spans, and a price point has to be a
meaningful share of what that merchant charges. Fifteen results became five, with
none wrong.

### 5. Foreign spending is inferred, never looked up

**Decision.** Countries, currencies and conversion quality are all derived from
the statement itself. No exchange-rate API, no country database fetch.

**Why.** Decision 1 is load-bearing. The moment this calls out for a reference
rate, "no network" stops being true and the one test that proves it starts
failing. So the benchmark for "was this converted badly?" is the other
transactions in the same currency on the same statement, which all went through
the same card network within days of each other.

**Cost.** It only works with enough transactions in a currency to establish a
normal — four, by default. A single foreign charge cannot be judged, and is not.
It also measures *relative* badness: if every conversion on a trip was poor,
nothing stands out. A rate lookup would catch that. I decided the privacy
property was worth more than the edge case.

**How I'd know I was wrong.** If real statements routinely carry too few
transactions per currency for the comparison to fire, the feature is theatre.

**What checking this changed, twice.** I built the feature before confirming how
any of it works, which was the wrong order, and it cost me the same mistake in
two different ways.

First, the data. PDF statements print the original amount and rate beside each
foreign charge; most CSV exports drop both. My first version reported "0 lost to
bad conversions" against a CSV, indistinguishable from "your conversions were
fine" when the file had never carried the evidence.

Then, worse, the mechanism. I had assumed a point-of-sale conversion shows up as
a foreign charge converted at a poor rate, so I detected it by comparing implied
rates. It does not. When a cardholder accepts conversion at the till, the
transaction reaches the issuer *already in the home currency* — Mastercard's
merchant guide states the account is "debited using the exchange rate offered by
the acquirer", with "NO currency conversion details on cardholder statement". So
such a charge has no rate to compare, and a rate comparison can only ever examine
the charges that were not converted at the till. My detector was structurally
incapable of finding what it claimed to find.

My test suite passed throughout, because I had written the fixture from the same
assumption as the code. Planting a conversion that carried a local amount and a
bad rate created a transaction that does not occur, and then asserted the
detector found it. The test confirmed the assumption, not the behaviour.

That is the project's own named failure mode — a confident answer the data cannot
support — committed by me, in the feature whose README section is about avoiding
it. The lesson I'd take is narrower than "test more": **a fixture written from
the same belief as the code tests nothing.** Ground truth has to come from
outside, and for anything involving somebody else's system, that means reading
their documentation before writing the parser rather than after.

### 6. Read-only, and unable to act

**Decision.** The tool never writes to your source files and cannot touch your
accounts.

**Why.** The competition's headline feature is cancelling things for you. I chose
the opposite: a tool with no ability to move money can't move it wrongly, and
that makes it safe to point at your entire financial history without thinking
hard about it.

**Cost.** It stops at telling you. You still have to go and cancel the gym
yourself.

### 7. Tool results are budgeted for a context window

**Decision.** Every query returns aggregates by default; line-item detail is
capped at 200 rows and always reports when it truncated.

**Why.** This is specific to building for an AI rather than a screen. The limit
isn't pixels, it's tokens — and worse, a model handed 50 of 812 transactions will
happily summarise them as though they were all of them. So the cap is enforced
centrally, and every truncated result carries the real total so the model can say
"showing 50 of 812" instead of quietly being wrong.

**Cost.** Some questions need a second, narrower query rather than one big
answer. Worth it.

---

## What I got wrong on the way

Three bugs worth recording, because all three produced *confident wrong answers*
rather than errors, which is the category I now watch for:

**Dates.** `03/04/2026` is March 4th or April 3rd, and deciding per row scatters
a statement across the wrong months with no error anywhere. It's resolved once
per file now, using the whole column as evidence — a single `25/03` settles every
other row in the file.

**Credit card signs.** A card prints a purchase as positive and your bill payment
as negative, the reverse of a current account. Read it the obvious way and the
tool reports paying your Amex bill as spending and your spending as income. Then
I got it wrong a second, subtler way: the printed sign *restates* the section
heading's direction rather than modifying it, so applying both re-inverts
everything.

**Two-letter country codes.** `IN` is India and Indiana; `CA` is Canada and
California. The first version read every Indianapolis purchase as foreign. The
fix was not a better lookup table — it was accepting that the code alone is not
evidence, and requiring something else on the row to corroborate it.

**A latching skip flag.** Statements are laid out Section → Summary → Detail.
Skipping the summary is correct; forgetting to stop skipping silently returned 4
transactions out of 35 and looked like it had worked.

The pattern across all three: the dangerous failures weren't crashes, they were
plausible output. That's why the PDF path reconciles against the statement's own
totals, and why the test suite asserts against planted ground truth rather than
against whatever the code happened to produce on the day.
