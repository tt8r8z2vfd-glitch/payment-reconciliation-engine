"""
Transaction reconciliation engine, v2.

    python main.py --gateway data/gateway_export.csv \
                   --bank data/bank_statement.csv \
                   --out output/reconciliation_report.xlsx
"""

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))

import classify
import loaders
import matchers
import report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="data/gateway_export.csv")
    ap.add_argument("--bank", default="data/bank_statement.csv")
    ap.add_argument("--out", default="output/reconciliation_report.xlsx")
    args = ap.parse_args()

    t0 = time.time()

    print("Loading   ...", end=" ", flush=True)
    gw = loaders.load_gateway(args.gateway)
    bk = loaders.load_bank(args.bank)
    print(f"{len(gw):,} gateway / {len(bk):,} bank rows")

    print("Matching  ...")
    r = matchers.run(gw, bk)
    match_seconds = time.time() - t0

    pairs = r["pairs"]
    layer_counts = Counter(pairs["layer"])
    for layer in sorted(layer_counts):
        print(f"   {layer:<20} {layer_counts[layer]:>7,}")
    print(f"   {'Batch netted':<20} {r['batch_payments']:>7,} "
          f"across {len(r['batches'][r['batches']['Payments Matched'] > 0]):,} settlements")
    print(f"   {'Reversals linked':<20} {len(r['reversals']):>7,}")
    print(f"   {'Duplicates retired':<20} {len(r['duplicates']):>7,}")
    print(f"   {'Held for review':<20} {len(r['review']):>7,}")

    exact = pairs[pairs["layer"] == "L1 Exact"]
    tol = pairs[pairs["layer"] != "L1 Exact"]
    matched_df = _pair_frame(gw, bk, exact, with_basis=False)
    tol_df = _pair_frame(gw, bk, tol, with_basis=True)

    un_gw = classify.gateway_side(gw, r["unmatched_gw"])
    un_bk = classify.bank_side(bk, r["unmatched_bk"])
    reason_counts = Counter(list(un_gw["Likely Reason"]) + list(un_bk["Likely Reason"]))

    accounted = len(pairs) + r["batch_payments"] + len(r["duplicates"])
    chart_counts = dict(layer_counts)
    if r["batch_payments"]:
        chart_counts["Batch netted (N:1)"] = r["batch_payments"]

    stats = {
        "n_gw": len(gw),
        "n_bk": len(bk),
        "gw_value": round(float(gw["amount"].sum()), 2),
        "bk_value": round(float(bk["amount"].sum()), 2),
        "fee_total": round(float(tol_df["Difference"].clip(lower=0).sum())
                           + float(r["batches"]["Aggregate Fees"].clip(lower=0).sum()), 2),
        "batch_payments": r["batch_payments"],
        "n_reversals": len(r["reversals"]),
        "n_duplicates": len(r["duplicates"]),
        "n_review": len(r["review"]),
        "period": f"{gw['date'].min():%d %b %Y} to {gw['date'].max():%d %b %Y}",
        "runtime": match_seconds,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    report.build(args.out, stats, matched_df, tol_df, un_gw, un_bk,
                 chart_counts, dict(reason_counts.most_common()),
                 batches=r["batches"], reversals=r["reversals"],
                 duplicates=r["duplicates"], review=r["review"])

    print(f"\nAccounted {accounted:,} of {len(gw):,} gateway records "
          f"({accounted / len(gw):.1%})")
    print(f"Open      {len(un_gw):,} gateway / {len(un_bk):,} bank")
    print(f"Report    {args.out}")
    print(f"Matching  {match_seconds:.1f}s   total {time.time() - t0:.1f}s")


def _pair_frame(gw, bk, pairs, with_basis):
    cols = ["Transaction ID", "Gateway Date", "Gateway Amount", "Bank Reference",
            "Bank Date", "Bank Amount", "Difference"]
    if with_basis:
        cols.append("Match Basis")
    cols.append("Confidence")
    if len(pairs) == 0:
        return pd.DataFrame(columns=cols)

    g = gw.loc[pairs["gw_idx"]].reset_index(drop=True)
    b = bk.loc[pairs["bk_idx"]].reset_index(drop=True)
    p = pairs.reset_index(drop=True)

    data = {
        "Transaction ID": g["transaction_id"],
        "Gateway Date": g["ts"],
        "Gateway Amount": g["amount"],
        "Bank Reference": b["Reference"],
        "Bank Date": b["ts"],
        "Bank Amount": b["amount"],
        "Difference": (g["amount"] - b["amount"]).round(2),
    }
    if with_basis:
        data["Match Basis"] = p["reason"]
    data["Confidence"] = p["confidence"]
    return pd.DataFrame(data)


if __name__ == "__main__":
    main()
