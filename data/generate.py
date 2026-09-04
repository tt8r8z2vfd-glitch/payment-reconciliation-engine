"""
Synthetic data generator, v2.

v1 produced two files that disagreed on formats, fees, cut-off timing and
reference schemes. v2 adds the structural cases that make real interbank
reconciliation hard, because they break the assumption that one payment
produces one bank line:

  * Batch net settlement   — hundreds of small payments arrive as ONE credit
  * Reversals and returns  — a booking and its mirror image, both present
  * Channel routing        — value decides whether a payment settles same-day
  * Business-day calendar  — Friday evening lands on Monday, not Saturday

No real data is used anywhere in this project.
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from faker import Faker

fake = Faker()
Faker.seed(20240315)
random.seed(20240315)

N_TRANSACTIONS = 50_000
START = datetime(2024, 3, 1)  # noqa: DTZ001 - business date, not an instant
DAYS = 31

# Value threshold that routes a payment to the real-time channel rather than
# the end-of-day batch. Set low for the demo so both paths carry volume.
HVPS_THRESHOLD = 5_000.00

HOLIDAYS = ["2024-03-29", "2024-04-01"]   # Good Friday, Easter Monday

MIX = {
    "exact_id":      50,   # bank reference embeds the txn id, amount agrees
    "fee_deducted":  11,   # bank credits net of an acquiring fee
    "ref_only":       6,   # only the customer reference survives
    "timing_shift":   3,   # generic batch description, settled late
    "batch_settled": 24,   # no individual bank line at all
    "reversed":       2,   # booked, then reversed or returned
    "gateway_only":   4,   # still in flight at cut-off
}

BANK_ONLY_ROWS = 480
DUPLICATE_ROWS = 120

RETURN_CODES = {
    "AC01": "INCORRECT ACCOUNT NUMBER",
    "AC04": "ACCOUNT CLOSED",
    "AM05": "DUPLICATION",
    "BE01": "NAME MISMATCH",
}

OUT = Path(__file__).parent


def next_business_day(d, n=1):
    result = np.busday_offset(d.date(), n, roll="forward", holidays=HOLIDAYS)
    return datetime.combine(result.astype("datetime64[D]").astype(object),
                            datetime.min.time())


def _fate_pool():
    pool = []
    for name, pct in MIX.items():
        pool.extend([name] * int(N_TRANSACTIONS * pct / 100))
    while len(pool) < N_TRANSACTIONS:
        pool.append("exact_id")
    random.shuffle(pool)
    return pool


def _fee_for(amount):
    return round(amount * random.choice([0.019, 0.022, 0.025, 0.029]) + 0.30, 2)


def build():
    gateway_rows, bank_rows = [], []
    batches = {}
    fates = _fate_pool()

    for i in range(N_TRANSACTIONS):
        fate = fates[i]

        day_offset = random.randint(0, DAYS - 1)
        hour = random.randint(22, 23) if fate == "timing_shift" else random.randint(6, 21)
        ts = START + timedelta(days=day_offset, hours=hour,
                               minutes=random.randint(0, 59),
                               seconds=random.randint(0, 59))

        amount = min(round(random.lognormvariate(4.2, 1.1) + 5, 2), 25_000.00)
        if fate == "batch_settled":
            amount = min(amount, HVPS_THRESHOLD - 0.01)

        channel = "HVPS" if amount >= HVPS_THRESHOLD else "BEPS"

        if channel == "HVPS":
            settle = ts.replace(hour=0, minute=0, second=0)
        else:
            settle = next_business_day(ts, 1)

        txn_id = f"TXN{ts:%Y%m%d}{i:06d}"
        customer_ref = f"CUST-{random.randint(1000, 9999)}"

        gateway_rows.append({
            "transaction_id": txn_id,
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "amount": f"{amount:.2f}",
            "currency": "USD",
            "status": "settled" if fate != "gateway_only" else "pending_settlement",
            "channel": channel,
            "expected_settlement": settle.strftime("%Y-%m-%d"),
            "customer_ref": customer_ref,
        })

        if fate == "gateway_only":
            continue

        if fate == "batch_settled":
            key = settle.strftime("%Y-%m-%d")
            slot = batches.setdefault(key, {"gross": 0.0, "count": 0, "fees": 0.0})
            slot["gross"] += amount
            slot["count"] += 1
            slot["fees"] += _fee_for(amount)
            continue

        credit, description = amount, ""
        bank_date = settle

        if fate in ("exact_id", "reversed"):
            description = f"PAYMENT SETTLEMENT {txn_id}"
        elif fate == "fee_deducted":
            credit = round(amount - _fee_for(amount), 2)
            description = f"CARD SETTLEMENT NET {txn_id}"
        elif fate == "ref_only":
            description = f"MERCHANT CREDIT {customer_ref.replace('-', '')}"
        elif fate == "timing_shift":
            bank_date = next_business_day(settle, 1)
            description = f"SETTLEMENT BATCH {bank_date:%m%d}"

        original_ref = f"REF-{bank_date:%Y%m%d}-{random.randint(100000, 999999)}"
        bank_rows.append({
            "Reference": original_ref,
            "Date": bank_date.strftime("%d/%m/%Y"),
            "Debit": "",
            "Credit": f"{credit:.2f}",
            "Description": description,
        })

        if fate == "reversed":
            rev_date = next_business_day(bank_date, random.randint(1, 3))
            if random.random() < 0.45:
                code = random.choice(list(RETURN_CODES))
                desc = (f"RETURNED PAYMENT ORIG {original_ref} "
                        f"RSN {code} {RETURN_CODES[code]}")
                debit = round(credit - round(random.uniform(0.5, 3.0), 2), 2)
            else:
                desc = f"REVERSAL OF {original_ref}"
                debit = credit
            bank_rows.append({
                "Reference": f"REF-{rev_date:%Y%m%d}-{random.randint(100000, 999999)}",
                "Date": rev_date.strftime("%d/%m/%Y"),
                "Debit": f"{debit:.2f}",
                "Credit": "",
                "Description": desc,
            })

    for key, slot in batches.items():
        # Settlement dates are business dates, not instants. A timezone here
        # would imply a precision the clearing system does not have.
        d = datetime.strptime(key, "%Y-%m-%d")  # noqa: DTZ007
        net = round(slot["gross"] - slot["fees"], 2)
        bank_rows.append({
            "Reference": f"REF-{d:%Y%m%d}-{random.randint(100000, 999999)}",
            "Date": d.strftime("%d/%m/%Y"),
            "Debit": "",
            "Credit": f"{net:.2f}",
            "Description": f"BATCH SETTLEMENT B{d:%Y%m%d} COUNT {slot['count']}",
        })

    kinds = [("REFUND PROCESSED", "debit"), ("CHARGEBACK RECEIVED", "debit"),
             ("MONTHLY SERVICE CHARGE", "debit"), ("MANUAL ADJUSTMENT", "credit"),
             ("INTEREST CREDIT", "credit")]
    for _ in range(BANK_ONLY_ROWS):
        label, side = random.choice(kinds)
        d = START + timedelta(days=random.randint(0, DAYS - 1))
        val = round(random.uniform(12, 900), 2)
        bank_rows.append({
            "Reference": f"REF-{d:%Y%m%d}-{random.randint(100000, 999999)}",
            "Date": d.strftime("%d/%m/%Y"),
            "Debit": f"{val:.2f}" if side == "debit" else "",
            "Credit": f"{val:.2f}" if side == "credit" else "",
            "Description": f"{label} {fake.bothify('##??').upper()}",
        })

    for row in random.sample(gateway_rows, DUPLICATE_ROWS):
        dupe = dict(row)
        dupe["transaction_id"] = row["transaction_id"][:-1] + "9"
        gateway_rows.append(dupe)

    random.shuffle(gateway_rows)
    random.shuffle(bank_rows)

    _write(OUT / "gateway_export.csv", gateway_rows,
           ["transaction_id", "timestamp", "amount", "currency", "status",
            "channel", "expected_settlement", "customer_ref"])

    balance = 250_000.00
    for r in bank_rows:
        balance += float(r["Credit"] or 0) - float(r["Debit"] or 0)
        r["Balance"] = f"{balance:.2f}"

    _write(OUT / "bank_statement.csv", bank_rows,
           ["Reference", "Date", "Debit", "Credit", "Description", "Balance"])

    print(f"gateway_export.csv   {len(gateway_rows):>7,} rows")
    print(f"bank_statement.csv   {len(bank_rows):>7,} rows "
          f"({len(batches)} batch settlements covering "
          f"{sum(b['count'] for b in batches.values()):,} payments)")


def _write(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    build()
