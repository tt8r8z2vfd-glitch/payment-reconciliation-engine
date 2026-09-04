"""
Settlement calendar and fee bands.

Two things v1 got wrong by treating them as constants.

Calendar: v1 measured settlement lag in calendar days. A payment captured on
Friday evening settles on Monday, three calendar days later but one business
day. A fixed calendar window is therefore either too tight on weekends or too
loose midweek, and both failures are silent.

Fees: v1 accepted any shortfall inside a single 3.5% ceiling. Acquiring
pricing is a percentage plus a fixed component, so on a $5 payment a $0.30
fixed fee is 6% and gets rejected, while on a $20,000 payment 3.5% is $700 of
slack that will happily swallow a wrong match. Bands fix both ends.
"""

from datetime import datetime

import numpy as np

HOLIDAYS = ["2024-03-29", "2024-04-01"]

# (upper bound of amount, max rate, max fixed component)
FEE_TIERS = [
    (100.00,        0.045, 0.35),
    (1_000.00,      0.032, 0.35),
    (10_000.00,     0.028, 0.50),
    (float("inf"),  0.022, 2.00),
]

# Business days a payment may take to settle, by channel.
SETTLEMENT_WINDOW = {
    "HVPS": (0, 0),    # real-time gross settlement, same day
    "BEPS": (0, 2),    # end-of-day batch, next business day plus slack
    None:   (0, 2),
}


def business_day_gap(d1, d2):
    """Business days from d1 to d2. Negative if d2 precedes d1."""
    return int(np.busday_count(_as_date(d1), _as_date(d2), holidays=HOLIDAYS))


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return getattr(d, "date", lambda: d)()


def max_fee(amount):
    """Largest shortfall that is still explainable as a processing fee."""
    a = abs(amount)
    for ceiling, rate, fixed in FEE_TIERS:
        if a <= ceiling:
            return a * rate + fixed
    return a * FEE_TIERS[-1][1] + FEE_TIERS[-1][2]


def fee_plausible(gross, net):
    gap = gross - net
    return 0 < gap <= max_fee(gross)


def within_window(channel, gap_days):
    lo, hi = SETTLEMENT_WINDOW.get(channel, SETTLEMENT_WINDOW[None])
    return lo <= gap_days <= hi
