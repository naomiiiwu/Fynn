"""
Reconciliation service for Shopee payout verification.

Computes expected payout from transaction components and compares
it to the actual settlement amount, flagging discrepancies > 2%.
"""

from typing import List

from models.transaction import ReconciliationResult, Transaction, TransactionType

DISCREPANCY_THRESHOLD_PCT = 2.0  # flag if discrepancy exceeds this %


def _detect_source_currency(transactions: List[Transaction]) -> str:
    """Return the most common currency across the transaction list."""
    from collections import Counter
    counts = Counter(t.currency for t in transactions if t.currency)
    return counts.most_common(1)[0][0] if counts else "MYR"


def reconcile(transactions: List[Transaction]) -> ReconciliationResult:
    """
    Reconcile a list of Shopee transactions and verify the settlement payout.

    Logic:
        expected_payout = gross_sales - refunds - platform_fees - shipping - vouchers

    Args:
        transactions: All transactions for the period, including the settlement entry.

    Returns:
        ReconciliationResult with computed totals and discrepancy flags.
    """
    src = _detect_source_currency(transactions)
    print(f"\n[Reconciliation] Starting payout reconciliation (source currency: {src})...")

    gross_sales: float = 0.0
    total_refunds: float = 0.0
    total_platform_fees: float = 0.0
    total_shipping: float = 0.0
    total_vouchers: float = 0.0
    actual_payout: float = 0.0

    for txn in transactions:
        if txn.type == TransactionType.ORDER:
            gross_sales += txn.amount_myr
        elif txn.type == TransactionType.REFUND:
            total_refunds += abs(txn.amount_myr)
        elif txn.type == TransactionType.PLATFORM_FEE:
            total_platform_fees += abs(txn.amount_myr)
        elif txn.type == TransactionType.SHIPPING:
            # Use signed amount: fees are negative (cost), rebates/vouchers are positive (credit)
            total_shipping -= txn.amount_myr
        elif txn.type == TransactionType.VOUCHER:
            total_vouchers += abs(txn.amount_myr)
        elif txn.type == TransactionType.SETTLEMENT:
            actual_payout = txn.amount_myr

    expected_payout = gross_sales - total_refunds - total_platform_fees - total_shipping - total_vouchers
    discrepancy = actual_payout - expected_payout
    discrepancy_pct = (abs(discrepancy) / expected_payout * 100) if expected_payout != 0 else 0.0
    is_flagged = discrepancy_pct > DISCREPANCY_THRESHOLD_PCT

    print(f"  Gross Sales:       {src} {gross_sales:>10,.2f}")
    print(f"  Refunds:         - {src} {total_refunds:>10,.2f}")
    print(f"  Platform Fees:   - {src} {total_platform_fees:>10,.2f}")
    print(f"  Shipping:        - {src} {total_shipping:>10,.2f}")
    print(f"  Vouchers:        - {src} {total_vouchers:>10,.2f}")
    print(f"  Expected Payout:   {src} {expected_payout:>10,.2f}")
    print(f"  Actual Payout:     {src} {actual_payout:>10,.2f}")
    print(f"  Discrepancy:       {src} {discrepancy:>10,.2f} ({discrepancy_pct:.2f}%)")

    if is_flagged:
        print(f"  ⚠️  DISCREPANCY FLAGGED — {discrepancy_pct:.2f}% exceeds {DISCREPANCY_THRESHOLD_PCT}% threshold!")
    else:
        print(f"  ✅ Payout reconciled within acceptable range ({discrepancy_pct:.2f}% discrepancy).")

    return ReconciliationResult(
        source_currency=src,
        gross_sales_myr=round(gross_sales, 2),
        total_refunds_myr=round(total_refunds, 2),
        total_platform_fees_myr=round(total_platform_fees, 2),
        total_shipping_myr=round(total_shipping, 2),
        total_vouchers_myr=round(total_vouchers, 2),
        expected_payout_myr=round(expected_payout, 2),
        actual_payout_myr=round(actual_payout, 2),
        discrepancy_myr=round(discrepancy, 2),
        discrepancy_pct=round(discrepancy_pct, 4),
        is_discrepancy_flagged=is_flagged,
    )
