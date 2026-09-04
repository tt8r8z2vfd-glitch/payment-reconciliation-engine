"""
Give every open item a reason.

"Unmatched" is not an answer. Whoever opens the report needs to know which
open items are expected consequences of how the systems work, and which are
genuine breaks worth someone's afternoon.
"""

import pandas as pd


def gateway_side(gw, idxs):
    rows = []
    for i in idxs:
        status = gw.at[i, "status"]
        channel = gw.at[i, "channel"]
        if status != "settled":
            reason = "Not yet settled at statement cut-off"
        elif channel == "BEPS":
            reason = ("Expected in a batch settlement that could not be "
                      "reconciled - see Batch Settlements")
        else:
            reason = "No corresponding bank credit found"
        rows.append({
            "Transaction ID": gw.at[i, "transaction_id"],
            "Date": gw.at[i, "ts"],
            "Amount": gw.at[i, "amount"],
            "Channel": channel,
            "Expected Settlement": gw.at[i, "settle_date"],
            "Status": status,
            "Likely Reason": reason,
        })
    return pd.DataFrame(rows)


def bank_side(bk, idxs):
    keywords = {
        "BATCH SETTLEMENT": "Batch credit that did not reconcile to a cohort",
        "REFUND": "Refund issued, no matching gateway capture",
        "CHARGEBACK": "Chargeback, handled outside the gateway",
        "SERVICE CHARGE": "Bank fee, not a customer payment",
        "INTEREST": "Interest credit, not a customer payment",
        "MANUAL ADJUSTMENT": "Manual entry by the bank",
    }
    rows = []
    for i in idxs:
        desc = bk.at[i, "Description"].upper()
        reason = next((v for k, v in keywords.items() if k in desc),
                      "Unidentified bank movement, needs review")
        rows.append({
            "Reference": bk.at[i, "Reference"],
            "Date": bk.at[i, "ts"],
            "Amount": bk.at[i, "amount"],
            "Description": bk.at[i, "Description"],
            "Likely Reason": reason,
        })
    return pd.DataFrame(rows)
