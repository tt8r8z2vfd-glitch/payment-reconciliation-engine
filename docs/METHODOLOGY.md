# Reconciliation Methodology

A longer explanation of what this engine does and why it is built this way.
The README covers what it produces; this covers the reasoning, and is the
document to read if you are deciding whether the approach fits your data.

---

## The premise

Reconciliation is usually described as a matching problem. That framing is
what produces tools nobody trusts.

Two systems that record the same payments will disagree. They disagree about
dates, because one records capture and the other records settlement. They
disagree about amounts, because fees come off in between. They disagree about
identifiers, because each system was designed without reference to the other.
And they disagree about *how many rows a payment is worth* — one payment can
produce two bank lines, or four hundred payments can produce one.

So the real problem is not matching. It is deciding which disagreements are
explainable and which are breaks. A tool that reports a 96% match rate and
leaves 4% in an undifferentiated pile has not done the second half of the job,
which is the half a human was going to spend their week on.

Every design decision below follows from that.

---

## Pipeline order is a correctness property, not an optimisation

The stages run in a fixed order because each one removes a class of rows that
would mislead the next. Reordering them does not just change performance; it
changes the answers.

### Stage 0a — Reversal and return linking

A reversal is a mirror debit that cancels an earlier credit. A return is the
same shape with a reason code attached, arriving days later when the receiving
bank rejects the payment.

Both must be paired off *before* matching starts. Left in place, a reversed
payment produces two exceptions on opposite sides of the report: an
unexplained debit, and a credit that appears to have no counterpart.

**The trap:** the obvious implementation retires both lines. That is wrong.
The original credit was a real settlement of a real gateway payment, and it
still has to tie back to it. Retiring both orphans the payment and reports it
as missing. Only the mirror debit is retired here; the credit stays in play.

Return reason codes (`AC01` incorrect account, `AC04` account closed, `AM05`
duplication, `BE01` name mismatch) are extracted and reported, because "why
are returns up this month" is the first question anyone asks.

### Stage 0b — Duplicate detection

A payment exported twice under near-identical identifiers. Fingerprinted on
customer reference, amount and capture date; the earliest transaction id wins
and later ones are retired with a pointer back to it.

This has to happen before batch netting. A duplicate that survives into a
batch cohort inflates its gross, which pushes the implied fee outside its
plausible band, which fails a batch that was actually fine — and takes several
hundred correctly settled payments down with it.

### Stage 1 — Exact

Transaction id present in the bank narrative, amounts agreeing to the cent.
Cheap, unambiguous, and it clears the large majority of volume so that later
stages work against a small residual.

### Stage 2 — Fee tolerance, banded

Same id, bank short by a plausible processing fee.

**What "plausible" means matters.** A single percentage ceiling fails at both
ends. Acquiring pricing is a rate plus a fixed component, so on a $5 payment a
$0.30 fixed fee is 6% and a 3.5% ceiling rejects a perfectly ordinary match.
On a $20,000 payment, 3.5% is $700 of slack that will happily swallow a wrong
match and report it with confidence.

Bands, by value tier:

| Value up to | Max rate | Max fixed |
|---|---|---|
| $100 | 4.5% | $0.35 |
| $1,000 | 3.2% | $0.35 |
| $10,000 | 2.8% | $0.50 |
| above | 2.2% | $2.00 |

The computed fee is written into the report, not just used as a threshold. A
match that "passed tolerance" without saying how much tolerance it used is not
auditable.

### Stage 2b — Reference corroborated

Where the transaction id did not survive into the bank narrative, the customer
reference plus amount and a settlement-window check.

### Stage 3 — Fuzzy, with an ambiguity guard

Amount equality inside a business-day window.

**The guard is the point.** When two or more candidates are equally
consistent, the engine matches neither. It writes the case to a review sheet
with the top candidates listed and says why it stopped.

Picking the nearest and moving on would raise the headline match rate. It
would also mean a percentage of the matches in the report are guesses
presented as facts, and nobody downstream would know which ones. An
unexplained match is as much a problem as an unmatched row.

### Stage 4 — Batch netting (N:1)

Hundreds of small payments arrive as a single bank credit, net of aggregate
fees. This is how end-of-day batch channels work, and how every major
processor pays out.

**Why this stage runs last.** A cohort keyed on settlement date alone
over-collects: payments that merely settled late through their own channel
share that date without belonging to the batch. No amount of arithmetic
separates them, because both look like ordinary payments of ordinary value.

Clearing everything with its own bank line first leaves a residual that is, by
construction, close to the batch membership. This single ordering change moved
batch reconciliation from 0 of 21 settlements to 18 of 21 on the sample data.

Two independent constraints then validate the cohort, both supplied by the
bank: the payment count stated in its narrative, and the value it credited.
Where the cohort exceeds the stated count, a greedy trim removes the surplus —
choosing at each step the payment closest to the average value still to be
shed, rather than simply dropping the smallest, which would satisfy the count
and leave the value badly wrong.

Where a batch cannot be reconciled, it is reported as a batch that did not
reconcile, with the implied fee rate quantified. Its members stay open. The
alternative — dumping four hundred payments into the exception pile with no
indication they belong together — is technically an accurate statement that
nobody can act on.

---

## The settlement calendar

Settlement lag is measured in business days, not calendar days.

A payment captured on Friday evening settles Monday: three calendar days, one
business day. A fixed calendar window is therefore too tight across weekends
and too loose midweek, and both failures are silent — one produces phantom
exceptions, the other produces wrong matches.

Windows are per channel, because the channel determines the timing:

| Channel | Routing | Window |
|---|---|---|
| Real-time gross settlement | above the value threshold | same business day |
| End-of-day batch | below the threshold | next business day, plus slack |

Holidays are configurable in `src/settlement.py`.

---

## What the engine will not do

**It will not guess.** Ambiguous cases go to a review sheet. The match rate is
lower than it could be, and the matches that are there can be relied on.

**It will not silently absorb.** A batch whose value cannot be explained is
reported as unreconciled, not forced through with a widened tolerance.

**It will not report an exception without a reason.** Every open item on both
sides carries a probable cause. Some are expected consequences of how the
systems work — not yet settled, bank charge, chargeback — and some are genuine
breaks. Separating those two is most of the value.

---

## Adapting it

The pieces designed to be changed:

| What | Where |
|---|---|
| Fee bands | `FEE_TIERS` in `src/settlement.py` |
| Channel windows | `SETTLEMENT_WINDOW` in `src/settlement.py` |
| Holiday calendar | `HOLIDAYS` in `src/settlement.py` |
| Batch fee expectations | `EXPECTED_BATCH_FEE`, `BATCH_TOLERANCE_RATE` in `src/matchers.py` |
| Ambiguity sensitivity | `AMBIGUITY_MARGIN` in `src/matchers.py` |
| Input formats | `src/loaders.py` — the only module that knows about file layouts |

Adding a source means writing one loader that returns the common shape. The
matching stages never see a file format.

---

## A note on the data

Everything in this repository is generated. The author's production experience
with interbank payment and clearing systems is covered by confidentiality
agreements, so the sample data was built to reproduce the same failure modes:
batch net settlement, reversals and returns, value-based channel routing,
business-day calendars, fee deduction, and single-sided records.
