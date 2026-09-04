"""Tiered transaction matching pipeline.

This module contains the reconciliation logic. It receives two normalised
frames from :mod:`loaders` and decides which gateway records correspond to
which bank records.

Pipeline ordering
-----------------
The stages run in a fixed order, and that order is a correctness property
rather than a performance optimisation. Each stage removes a class of rows
that would otherwise mislead the stage after it:

===== ============================= =========================================
Stage Name                          Removes
===== ============================= =========================================
0a    Reversal linking              Mirror debits that cancel earlier credits
0b    Duplicate detection           Second exports of the same payment
1     Exact                         Transaction id present on both sides
2     Fee tolerance                 Same id, bank short by a plausible fee
2b    Reference corroborated        Customer reference plus amount and date
3     Fuzzy with ambiguity guard    Amount inside a business-day window
4     Batch netting (N:1)           Cohorts that settled as a single credit
===== ============================= =========================================

Stage 4 runs last deliberately. See :func:`match_batches` for why.

Bookkeeping
-----------
Two integer sets, ``gw_used`` and ``bk_used``, hold the positional indices
already consumed. Every stage skips indices present in them and adds the ones
it claims. An index is never released once claimed, so a match made by an
earlier, more reliable stage is never overturned by a later, weaker one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import pandas as pd

from settlement import business_day_gap, fee_plausible, within_window

# --------------------------------------------------------------------------
# Tuning constants
# --------------------------------------------------------------------------

#: Float comparison threshold for "the same amount". Currency arithmetic in
#: float64 drifts below a tenth of a cent; half a cent sits comfortably above
#: that drift and below any real difference.
CENT: float = 0.005

#: Business days that must separate the best fuzzy candidate from the
#: runner-up before a match is made automatically. Below this the case is
#: routed to manual review instead of guessed.
AMBIGUITY_MARGIN: int = 1

#: Widest aggregate fee, as a fraction of gross, that a whole batch may carry
#: before the engine refuses to call it reconciled.
BATCH_TOLERANCE_RATE: float = 0.045

#: Typical blended fee on a batch. Used to aim the cohort trim at the gross
#: the credit most likely came from. Aiming at ``BATCH_TOLERANCE_RATE``
#: instead sheds too much value and leaves every batch reporting an
#: implausible fee rate.
EXPECTED_BATCH_FEE: float = 0.025

#: How far a cohort's gross may fall *below* the credited amount before the
#: batch is rejected. A small shortfall means one member is missing from the
#: export, which is worth reporting but not worth failing several hundred
#: otherwise reconciled payments over.
SHORTFALL_TOLERANCE: float = 0.01


# --------------------------------------------------------------------------
# Stage 0a — reversals and returns
# --------------------------------------------------------------------------
def link_reversals(bk: pd.DataFrame) -> tuple[pd.DataFrame, set[int]]:
    """Pair each mirror debit with the credit it cancels.

    A reversal is a debit that undoes an earlier credit; a return is the same
    shape with an ISO 20022 reason code attached, arriving days later when the
    receiving bank rejects the payment. Both must be paired off before
    matching starts, because a reversed payment left intact produces two
    exceptions on opposite sides of the report: an unexplained debit, and a
    credit that appears to have no counterpart.

    Only the debit is retired. The obvious implementation retires both lines,
    and that is wrong: the original credit settled a real gateway payment and
    still has to tie back to it. Retiring both orphans the payment and reports
    it as missing.

    Args:
        bk: Normalised bank frame. Requires the columns ``Reference``,
            ``is_reversal``, ``reversal_of``, ``amount``, ``ts`` and
            ``return_code``, all produced by :func:`loaders.load_bank`.

    Returns:
        A tuple of:

        * A frame with one row per linked pair, carrying both references,
          both amounts, the residual between them, and the return code where
          one was present.
        * The set of positional indices of the retired debits.
    """
    # Reference is unique per bank line, so a plain dict resolves the pointer
    # each reversal carries.
    index_by_reference: dict[str, int] = {
        reference: i for i, reference in enumerate(bk["Reference"])
    }

    retired: set[int] = set()
    pairs: list[dict[str, Any]] = []

    for debit_idx in bk.index[bk["is_reversal"]]:
        target_reference = bk.at[debit_idx, "reversal_of"]
        credit_idx = index_by_reference.get(target_reference)

        # A reversal may point at a line outside this statement period, and a
        # line already claimed by an earlier pair must not be claimed twice.
        if credit_idx is None or credit_idx in retired or debit_idx in retired:
            continue

        original = bk.at[credit_idx, "amount"]
        # Debits are negative in the normalised frame; flip for readability.
        mirror = -bk.at[debit_idx, "amount"]

        pairs.append({
            "Original Reference": target_reference,
            "Original Date": bk.at[credit_idx, "ts"],
            "Original Amount": original,
            "Reversal Reference": bk.at[debit_idx, "Reference"],
            "Reversal Date": bk.at[debit_idx, "ts"],
            "Reversal Amount": mirror,
            # Non-zero where the return carried a handling charge.
            "Residual": round(original - mirror, 2),
            "Return Code": bk.at[debit_idx, "return_code"] or "-",
            "Type": "Return" if bk.at[debit_idx, "return_code"] else "Reversal",
        })
        retired.add(debit_idx)

    return pd.DataFrame(pairs), retired


# --------------------------------------------------------------------------
# Stage 0b — duplicate exports
# --------------------------------------------------------------------------
def detect_duplicates(gw: pd.DataFrame) -> tuple[pd.DataFrame, set[int]]:
    """Retire second exports of the same payment.

    Runs before batch netting rather than after. A duplicate that survives
    into a batch cohort inflates its gross, which pushes the implied fee
    outside its plausible band, which fails a batch that was in fact fine and
    takes several hundred correctly settled payments down with it.

    The fingerprint is customer reference, amount and capture date.
    Transaction id is deliberately excluded: a genuine duplicate export
    differs precisely in that field.

    Args:
        gw: Normalised gateway frame. Requires ``transaction_id``,
            ``customer_ref``, ``amount``, ``date`` and ``ts``.

    Returns:
        A tuple of the duplicate rows, each pointing at the record it
        duplicates, and the set of positional indices retired.
    """
    first_seen: dict[tuple[str, float, Any], str] = {}
    duplicates: list[dict[str, Any]] = []
    retired: set[int] = set()

    # Iterate in transaction id order so the surviving record is deterministic.
    # Without this the winner depends on row order in the source file, and the
    # report changes between runs on identical input.
    for gi in sorted(range(len(gw)), key=lambda i: gw.at[i, "transaction_id"]):
        fingerprint = (
            gw.at[gi, "customer_ref"],
            round(gw.at[gi, "amount"], 2),
            gw.at[gi, "date"],
        )

        if fingerprint in first_seen:
            duplicates.append({
                "Transaction ID": gw.at[gi, "transaction_id"],
                "Date": gw.at[gi, "ts"],
                "Amount": gw.at[gi, "amount"],
                "Customer Ref": gw.at[gi, "customer_ref"],
                "Duplicate Of": first_seen[fingerprint],
                "Basis": "Same customer reference, amount and capture date",
            })
            retired.add(gi)
        else:
            first_seen[fingerprint] = gw.at[gi, "transaction_id"]

    return pd.DataFrame(duplicates), retired


# --------------------------------------------------------------------------
# Stage 4 helpers
# --------------------------------------------------------------------------
def _trim_to_fit(
    gw: pd.DataFrame,
    members: list[int],
    target_count: int,
    net_credited: float,
) -> tuple[list[int], list[int]]:
    """Reduce an over-collected batch cohort to the payments the bank settled.

    The bank supplies two independent constraints: how many payments it netted
    (stated in its narrative) and what it credited. A cohort keyed on
    settlement date alone satisfies neither exactly, because it also catches
    payments that merely settled that day through their own channel.

    Removing the smallest members would satisfy the count and leave the value
    badly wrong. Instead each removal takes the member closest to the average
    value still to be shed, which converges because the residual shrinks with
    every pass.

    Args:
        gw: Normalised gateway frame.
        members: Positional indices of the over-collected cohort.
        target_count: Payment count stated by the bank.
        net_credited: Amount the bank actually credited.

    Returns:
        A tuple of ``(kept, removed)`` index lists, where
        ``len(kept) == target_count``.

    Note:
        Greedy and linear in the surplus, not an exact subset-sum solution. An
        exact solve is NP-hard and, on cohorts of several hundred members with
        no distinguishing features, would not be more correct — only slower.
        The ordering described in :func:`match_batches` is what keeps this
        function's input small enough to be reliable.
    """
    surplus = len(members) - target_count
    gross = sum(gw.at[g, "amount"] for g in members)

    # Gross the credited amount most plausibly came from, at a typical fee.
    implied_gross = (net_credited + 1.0) / (1 - EXPECTED_BATCH_FEE)
    value_to_shed = max(gross - implied_gross, 0.0)

    pool = sorted(members, key=lambda g: gw.at[g, "amount"])
    removed: list[int] = []

    for step in range(surplus):
        removals_left = surplus - step
        # Aim each removal at an equal share of what is left to shed. Taking
        # the largest member instead would overshoot on the first pass; taking
        # the smallest would never converge.
        target_value = value_to_shed / removals_left if removals_left else 0.0

        pick = min(
            range(len(pool)),
            key=lambda k: abs(gw.at[pool[k], "amount"] - target_value),
        )
        value_to_shed = max(value_to_shed - gw.at[pool[pick], "amount"], 0.0)
        removed.append(pool.pop(pick))

    return pool, removed


def _batch_verdict(declared: Any, actual: int, gap: float) -> str:
    """Describe how well a reconciled cohort agrees with the bank's narrative.

    Args:
        declared: Payment count from the bank description, or NaN if absent.
        actual: Size of the cohort finally accepted.
        gap: Gross minus net credited, i.e. the implied aggregate fee.

    Returns:
        A short phrase for the ``Reconciles`` column of the report.
    """
    if gap < 0:
        return f"Short by {abs(gap):,.2f} - member missing from export"
    if pd.notna(declared) and actual != int(declared):
        return f"Count differs by {actual - int(declared):+d}"
    return "Yes"


# --------------------------------------------------------------------------
# Stage 4 — batch net settlement
# --------------------------------------------------------------------------
def match_batches(
    gw: pd.DataFrame,
    bk: pd.DataFrame,
    gw_open: Iterable[int],
    bk_open: Iterable[int],
) -> tuple[pd.DataFrame, set[int], set[int]]:
    """Match each batch credit against the cohort of payments that settled into it.

    End-of-day batch channels net hundreds of small payments into a single
    bank credit. This breaks the assumption underlying every other stage, that
    one payment produces one bank line.

    Why this stage runs last:
        The intuitive implementation keys the cohort on settlement date and
        runs early, before the residual has been thinned out. That
        over-collects. Payments that merely settled late through their own
        channel share the settlement date without belonging to the batch, and
        no amount of arithmetic separates them, because both are ordinary
        payments of ordinary value.

        Clearing everything that has its own bank line first leaves a residual
        that is, by construction, close to the batch membership. On the sample
        data this single ordering change moved batch reconciliation from 0 of
        21 settlements to 18 of 21, with no change to the algorithm itself.

    Args:
        gw: Normalised gateway frame.
        bk: Normalised bank frame.
        gw_open: Gateway indices not yet claimed by an earlier stage.
        bk_open: Bank indices not yet claimed by an earlier stage.

    Returns:
        A tuple of:

        * One row per batch credit, reconciled or not. Unreconciled batches
          are reported with their implied fee rate rather than omitted.
        * Gateway indices consumed.
        * Bank indices consumed.

    Note:
        A batch that fails the value test consumes nothing. Its members stay
        open and are reported individually, because a batch whose value cannot
        be explained has not been reconciled, and forcing it through on a
        widened tolerance would state something untrue.
    """
    results: list[dict[str, Any]] = []
    consumed_gw: set[int] = set()
    consumed_bk: set[int] = set()

    # Candidate members: unclaimed batch-channel payments the gateway believes
    # have settled. Pending payments are excluded — they have not reached any
    # batch yet.
    cohorts: dict[Any, list[int]] = defaultdict(list)
    for gi in gw_open:
        if gw.at[gi, "channel"] == "BEPS" and gw.at[gi, "status"] == "settled":
            cohorts[gw.at[gi, "settle_date"]].append(gi)

    for bi in bk_open:
        if not bk.at[bi, "batch_id"]:
            continue

        members = [g for g in cohorts.get(bk.at[bi, "date"], [])
                   if g not in consumed_gw]
        if not members:
            continue

        net = bk.at[bi, "amount"]
        declared = bk.at[bi, "batch_count"]
        excluded: list[int] = []

        if pd.notna(declared) and len(members) > int(declared):
            members, excluded = _trim_to_fit(gw, members, int(declared), net)

        gross = round(sum(gw.at[g, "amount"] for g in members), 2)
        gap = round(gross - net, 2)
        allowance = gross * BATCH_TOLERANCE_RATE + 2.0

        record: dict[str, Any] = {
            "Batch ID": bk.at[bi, "batch_id"],
            "Bank Reference": bk.at[bi, "Reference"],
            "Settlement Date": bk.at[bi, "ts"],
            "Payments Matched": len(members),
            "Count Declared": int(declared) if pd.notna(declared) else None,
            "Gross Value": gross,
            "Net Credited": net,
            "Aggregate Fees": gap,
            "Effective Fee Rate": round(gap / gross, 5) if gross else 0.0,
            "Held Back": len(excluded),
            "Reconciles": _batch_verdict(declared, len(members), gap),
        }

        if not (-net * SHORTFALL_TOLERANCE <= gap <= allowance):
            # Report it rather than silently dumping several hundred payments
            # into the exception pile with no indication they belong together.
            record["Payments Matched"] = 0
            record["Reconciles"] = (
                f"No - implied fee {gap / gross:.2%} outside band" if gross
                else "No - cohort empty"
            )
            results.append(record)
            continue

        results.append(record)
        consumed_gw.update(members)
        consumed_bk.add(bi)

    return pd.DataFrame(results), consumed_gw, consumed_bk


# --------------------------------------------------------------------------
# Full pipeline
# --------------------------------------------------------------------------
def run(gw: pd.DataFrame, bk: pd.DataFrame) -> dict[str, Any]:
    """Reconcile a gateway export against a bank statement.

    Args:
        gw: Normalised gateway frame from :func:`loaders.load_gateway`.
        bk: Normalised bank frame from :func:`loaders.load_bank`.

    Returns:
        A dict with the following keys:

        ``pairs``
            One row per one-to-one match: gateway index, bank index, the
            stage that produced it, a readable basis, and a confidence score.
        ``reversals``
            Linked reversal and return pairs.
        ``duplicates``
            Retired second exports.
        ``batches``
            One row per batch credit, reconciled or not.
        ``review``
            Cases held back as ambiguous, with their candidates.
        ``unmatched_gw``, ``unmatched_bk``
            Positional indices still open on each side.
        ``batch_payments``
            Payments accounted for inside reconciled batches.
    """
    matches: list[tuple[int, int, str, str, float]] = []
    gw_used: set[int] = set()
    bk_used: set[int] = set()

    # ---- Stage 0a: reversals and returns ---------------------------------
    reversals, retired_bk = link_reversals(bk)
    bk_used |= retired_bk

    # ---- Stage 0b: duplicate exports -------------------------------------
    duplicates, retired_gw = detect_duplicates(gw)
    gw_used |= retired_gw

    # ---- Stage 1: exact ---------------------------------------------------
    # Bank lines grouped by the transaction id embedded in their narrative.
    # Built once and reused by stage 2, which walks the same groups looking
    # for a different kind of agreement.
    bank_by_id: dict[str, list[int]] = defaultdict(list)
    for i, key in enumerate(bk["key_id"]):
        if key and i not in bk_used:
            bank_by_id[key].append(i)

    for gi, key in enumerate(gw["key_id"]):
        for bi in bank_by_id.get(key, []):
            if bi in bk_used:
                continue
            if abs(gw.at[gi, "amount"] - bk.at[bi, "amount"]) < CENT:
                matches.append((gi, bi, "L1 Exact", "ID and amount agree", 1.00))
                gw_used.add(gi)
                bk_used.add(bi)
                break

    # ---- Stage 2: banded fee tolerance ------------------------------------
    for gi, key in enumerate(gw["key_id"]):
        if gi in gw_used:
            continue
        for bi in bank_by_id.get(key, []):
            if bi in bk_used:
                continue
            gross = gw.at[gi, "amount"]
            net = bk.at[bi, "amount"]
            if fee_plausible(gross, net):
                # The computed fee goes into the report, not just the
                # threshold check. A match that passed tolerance without
                # saying how much tolerance it used is not auditable.
                rate = (gross - net) / gross
                basis = (f"Fee {gross - net:.2f} ({rate:.2%}), "
                         f"within band for {gross:,.0f}")
                matches.append((gi, bi, "L2 Fee tolerance", basis, 0.97))
                gw_used.add(gi)
                bk_used.add(bi)
                break

    # ---- Stage 2b: customer reference, corroborated -----------------------
    # Rebuilt rather than filtered: stages 1 and 2 have claimed a large share
    # of the bank side by now, and a stale index would mostly hold entries
    # this stage has to skip.
    bank_by_customer: dict[str, list[int]] = defaultdict(list)
    for i, key in enumerate(bk["key_cust"]):
        if key and i not in bk_used:
            bank_by_customer[key].append(i)

    for gi, key in enumerate(gw["key_cust"]):
        if gi in gw_used or not key:
            continue
        for bi in bank_by_customer.get(key, []):
            if bi in bk_used:
                continue
            # Customer reference is not unique, so amount and settlement
            # timing have to corroborate it before the pair is accepted.
            amount_agrees = abs(gw.at[gi, "amount"] - bk.at[bi, "amount"]) < CENT
            gap = business_day_gap(gw.at[gi, "settle_date"], bk.at[bi, "date"])
            if amount_agrees and within_window(gw.at[gi, "channel"], gap):
                matches.append((
                    gi, bi, "L2 Reference",
                    f"Customer ref, amount agrees, settled +{gap}bd", 0.95,
                ))
                gw_used.add(gi)
                bk_used.add(bi)
                break

    # ---- Stage 3: fuzzy, with an ambiguity guard --------------------------
    review: list[dict[str, Any]] = []

    # Batch credits are excluded: they are one-to-many by nature and stage 4
    # owns them. Letting fuzzy matching claim a batch credit for a single
    # payment would destroy the cohort before it is ever assembled.
    bank_by_amount: dict[float, list[int]] = defaultdict(list)
    for i in range(len(bk)):
        if i not in bk_used and bk.at[i, "amount"] > 0 and not bk.at[i, "batch_id"]:
            bank_by_amount[round(bk.at[i, "amount"], 2)].append(i)

    for gi in range(len(gw)):
        if gi in gw_used:
            continue

        channel = gw.at[gi, "channel"]
        candidates: list[tuple[int, int]] = []  # (business-day gap, bank index)

        for bi in bank_by_amount.get(round(gw.at[gi, "amount"], 2), []):
            if bi in bk_used:
                continue
            gap = business_day_gap(gw.at[gi, "settle_date"], bk.at[bi, "date"])
            if within_window(channel, gap):
                candidates.append((gap, bi))

        if not candidates:
            continue

        candidates.sort()

        # The guard. Two candidates equally consistent with the evidence mean
        # the evidence does not identify one of them. Picking the nearest
        # would raise the headline match rate and quietly fill the report with
        # guesses nobody downstream could tell apart from facts.
        if (len(candidates) > 1
                and candidates[1][0] - candidates[0][0] < AMBIGUITY_MARGIN):
            review.append({
                "Transaction ID": gw.at[gi, "transaction_id"],
                "Date": gw.at[gi, "ts"],
                "Amount": gw.at[gi, "amount"],
                "Channel": channel,
                "Candidates": len(candidates),
                "Top Candidates": ", ".join(
                    bk.at[b, "Reference"] for _, b in candidates[:3]
                ),
                "Why Held": "Two or more candidates equally consistent; "
                            "an automatic pick would be a guess",
            })
            continue

        gap, bi = candidates[0]
        matches.append((
            gi, bi, "L3 Fuzzy",
            f"Amount agrees, settled +{gap}bd, sole candidate", 0.90,
        ))
        gw_used.add(gi)
        bk_used.add(bi)

    # ---- Stage 4: batch netting, on the residual --------------------------
    gw_open = [i for i in range(len(gw)) if i not in gw_used]
    bk_open = [i for i in range(len(bk)) if i not in bk_used]
    batches, batch_gw, batch_bk = match_batches(gw, bk, gw_open, bk_open)
    gw_used |= batch_gw
    bk_used |= batch_bk

    pairs = pd.DataFrame(
        matches,
        columns=["gw_idx", "bk_idx", "layer", "reason", "confidence"],
    )

    return {
        "pairs": pairs,
        "reversals": reversals,
        "duplicates": duplicates,
        "batches": batches,
        "review": pd.DataFrame(review),
        "unmatched_gw": [i for i in range(len(gw)) if i not in gw_used],
        "unmatched_bk": [i for i in range(len(bk)) if i not in bk_used],
        "batch_payments": (
            int(batches["Payments Matched"].sum()) if len(batches) else 0
        ),
    }
