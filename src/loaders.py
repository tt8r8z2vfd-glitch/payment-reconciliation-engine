"""Read each side into a common internal shape, whatever format it arrives in."""

import pandas as pd

TXN_PATTERN = r"TXN\d{14}"
CUST_PATTERN = r"CUST\d{4}"
BATCH_PATTERN = r"BATCH SETTLEMENT (B\d{8}) COUNT (\d+)"
REVERSAL_PATTERN = r"(?:REVERSAL OF|ORIG)\s+(REF-\d{8}-\d{6})"
RSN_PATTERN = r"RSN ([A-Z]{2}\d{2})"


def load_gateway(path):
    df = pd.read_csv(path, dtype=str).fillna("")
    df["amount"] = df["amount"].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M:%S")
    df["date"] = df["ts"].dt.normalize()
    if "expected_settlement" in df:
        df["settle_date"] = pd.to_datetime(df["expected_settlement"])
    else:
        df["settle_date"] = df["date"]
    if "channel" not in df:
        df["channel"] = None
    df["key_id"] = df["transaction_id"]
    df["key_cust"] = df["customer_ref"].str.replace("-", "", regex=False)
    return df.reset_index(drop=True)


def load_bank(path):
    df = pd.read_csv(path, dtype=str).fillna("")
    df["credit"] = pd.to_numeric(df["Credit"].replace("", "0"))
    df["debit"] = pd.to_numeric(df["Debit"].replace("", "0"))
    df["amount"] = df["credit"] - df["debit"]
    df["ts"] = pd.to_datetime(df["Date"], format="%d/%m/%Y")
    df["date"] = df["ts"].dt.normalize()

    desc = df["Description"]
    df["key_id"] = desc.str.extract(f"({TXN_PATTERN})", expand=False).fillna("")
    df["key_cust"] = desc.str.extract(f"({CUST_PATTERN})", expand=False).fillna("")

    batch = desc.str.extract(BATCH_PATTERN)
    df["batch_id"] = batch[0].fillna("")
    df["batch_count"] = pd.to_numeric(batch[1], errors="coerce")

    df["reversal_of"] = desc.str.extract(REVERSAL_PATTERN, expand=False).fillna("")
    df["return_code"] = desc.str.extract(RSN_PATTERN, expand=False).fillna("")
    df["is_reversal"] = df["reversal_of"].ne("") & df["debit"].gt(0)
    return df.reset_index(drop=True)
