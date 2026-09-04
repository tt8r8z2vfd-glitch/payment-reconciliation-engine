# Transaction Reconciliation Engine

Matches a payment gateway export against a bank statement and explains every
line that does not tie out.

Reconciliation is usually described as a matching problem. In practice it is a
*disagreement* problem: the two files describe the same payments but disagree
about dates, amounts, references, and even how many rows a payment is worth.
The work is deciding which disagreements are explainable and which need a
human.

Full reasoning behind the design: **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**

---

## What it handles

| | Gateway export | Bank statement |
|---|---|---|
| Date format | `2024-03-15 14:23:11` | `15/03/2024` |
| Amount | `1250.00` gross | `1247.50` net of fee |
| Reference | `TXN20240315014154` | `REF-20240315-467459` |
| Small payments | 400 individual rows | **one** batch credit |
| Reversed payment | one row | two rows, days apart |
| Friday evening capture | 15 March | settles Monday, not Saturday |
| Coverage | in-flight payouts | refunds, chargebacks, bank charges |

---

## Pipeline

Order is a correctness property, not an optimisation. Each stage removes rows
that would mislead the next.

**0a. Reversal and return linking.** Pair a mirror debit with the credit it
cancels. Only the debit is retired — the original credit settled a real
payment and still has to tie back to it. Return reason codes (`AC01`, `AC04`,
`AM05`, `BE01`) are extracted and reported.

**0b. Duplicate detection.** Second exports of the same payment, retired
before they can inflate a batch cohort.

**1. Exact.** Transaction id on both sides, amounts agree to the cent.

**2. Fee tolerance, banded.** A single percentage ceiling fails at both ends:
on a $5 payment a $0.30 fixed fee is 6%, and on a $20,000 payment 3.5% is $700
of slack that swallows wrong matches. Four value tiers instead. The computed
fee is written into the report, not just used as a threshold.

**2b. Reference corroborated.** Customer reference plus amount, inside the
settlement window for that channel.

**3. Fuzzy, with an ambiguity guard.** Amount inside a business-day window.
When two candidates are equally consistent the engine matches **neither**,
and routes the case to a review sheet with candidates listed. Picking the
nearest would raise the headline match rate and quietly fill the report with
guesses.

**4. Batch netting (N:1).** One bank credit against the cohort of payments
that settled into it, validated against both constraints the bank supplies —
the count in its narrative and the value it credited. Runs last: a cohort
keyed on settlement date alone also catches payments that merely settled late,
and clearing everything with its own bank line first leaves a residual that is
the batch. That ordering took batch reconciliation from 0 of 21 settlements
to 18 of 21.

Settlement lag is counted in **business days**, per channel, with a
configurable holiday calendar.

---

## Output

| Sheet | Contents |
|---|---|
| **Summary** | Headline figures, matches by layer, exceptions by reason, charts |
| **Matched** | Exact pairs, both sides side by side |
| **Tolerance Matched** | Fee and fuzzy matches with the basis and confidence for each |
| **Batch Settlements** | Each batch credit, cohort size vs declared count, gross, net, implied fee rate, verdict |
| **Reversals** | Booking paired with its mirror, residual, return code |
| **Needs Review** | Cases held back as ambiguous, with candidates and why |
| **Duplicate Exports** | Second exports and what they duplicate |
| **Unmatched Gateway / Bank** | Open items with a probable cause on every row |

---

## Results on the sample data

```
Gateway records                50,120
Bank records                   37,501   (12,000 payments arrive as 21 batch credits)

Matched one-to-one             35,986
   L1 Exact                    26,000
   L2 Fee tolerance             5,487
   L2 Reference                 3,000
   L3 Fuzzy                     1,499
Settled inside a batch         10,874   across 18 of 21 settlements
Reversed or returned            1,000
Duplicate exports retired         120

Accounted for                  46,980   (93.7%)
Held for manual review              4
Open - gateway                  3,140   (2,000 in flight at cut-off)
Open - bank                       497

Matching                          8.1s
Report generation                  24s
```

Three batch settlements did not reconcile. They are reported as such, with the
implied fee rate quantified, rather than forced through on a widened
tolerance.

---

## Running it

```bash
pip install pandas openpyxl numpy faker

python data/generate.py     # build the sample files
python main.py              # reconcile and write the report
```

```bash
python main.py \
  --gateway data/gateway_export.csv \
  --bank    data/bank_statement.csv \
  --out     output/reconciliation_report.xlsx
```

---

## Layout

```
recon-engine/
├── data/generate.py       synthetic gateway and bank files
├── src/
│   ├── loaders.py         the only module that knows about file formats
│   ├── settlement.py      business-day calendar and fee bands
│   ├── matchers.py        the pipeline
│   ├── classify.py        a probable cause for every open item
│   └── report.py          Excel output
├── docs/METHODOLOGY.md
├── main.py
└── output/
```

Adding a source means writing one loader that returns the common shape. The
matching stages never see a file format.

---

## Note on the data

Everything here is generated. The author's production experience with
interbank payment and clearing systems is covered by confidentiality
agreements, so the sample data was built to reproduce the same failure modes.

---

## Code standards

- Type hints throughout, `from __future__ import annotations` for
  forward-compatible syntax.
- Google-style docstrings with `Args`, `Returns`, and `Note` sections. Where a
  design decision could reasonably have gone another way, the docstring says
  why it went this way.
- Comments explain *why*, not *what*. Every `noqa` carries its rationale.
- `ruff check` passes clean on `src/`, `data/` and `main.py`.

```bash
pip install ruff
ruff check src/ data/ main.py
```
