"""
Mock Shopee transaction data for ONE seller for March 2026.

Simulates a realistic month of activity including:
- 50 orders with varying MYR amounts
- 5 refunds
- Per-order platform fees (~5%)
- Shipping fee deductions
- One promotional voucher deduction
- Final settlement payout
"""

from datetime import datetime
from typing import List

from models.transaction import Transaction, TransactionType


def get_mock_transactions() -> List[Transaction]:
    """
    Return a list of realistic mock Shopee transactions for March 2026.

    All amounts are in MYR (Malaysian Ringgit).
    Positive amounts = money coming in (credits).
    Negative amounts = deductions (debits).
    """

    # --- 50 Orders ---
    orders = [
        Transaction(transaction_id="TXN-ORD-001", order_id="ORD-2026-001", date=datetime(2026, 3, 1, 9, 12), type=TransactionType.ORDER, description="Order payment received", amount_myr=85.00),
        Transaction(transaction_id="TXN-ORD-002", order_id="ORD-2026-002", date=datetime(2026, 3, 1, 11, 45), type=TransactionType.ORDER, description="Order payment received", amount_myr=120.50),
        Transaction(transaction_id="TXN-ORD-003", order_id="ORD-2026-003", date=datetime(2026, 3, 2, 8, 30), type=TransactionType.ORDER, description="Order payment received", amount_myr=45.00),
        Transaction(transaction_id="TXN-ORD-004", order_id="ORD-2026-004", date=datetime(2026, 3, 2, 14, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=210.00),
        Transaction(transaction_id="TXN-ORD-005", order_id="ORD-2026-005", date=datetime(2026, 3, 3, 10, 5), type=TransactionType.ORDER, description="Order payment received", amount_myr=67.90),
        Transaction(transaction_id="TXN-ORD-006", order_id="ORD-2026-006", date=datetime(2026, 3, 3, 16, 33), type=TransactionType.ORDER, description="Order payment received", amount_myr=155.00),
        Transaction(transaction_id="TXN-ORD-007", order_id="ORD-2026-007", date=datetime(2026, 3, 4, 9, 50), type=TransactionType.ORDER, description="Order payment received", amount_myr=98.00),
        Transaction(transaction_id="TXN-ORD-008", order_id="ORD-2026-008", date=datetime(2026, 3, 4, 13, 15), type=TransactionType.ORDER, description="Order payment received", amount_myr=300.00),
        Transaction(transaction_id="TXN-ORD-009", order_id="ORD-2026-009", date=datetime(2026, 3, 5, 10, 40), type=TransactionType.ORDER, description="Order payment received", amount_myr=75.50),
        Transaction(transaction_id="TXN-ORD-010", order_id="ORD-2026-010", date=datetime(2026, 3, 5, 15, 0), type=TransactionType.ORDER, description="Order payment received", amount_myr=189.00),
        Transaction(transaction_id="TXN-ORD-011", order_id="ORD-2026-011", date=datetime(2026, 3, 6, 8, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=55.00),
        Transaction(transaction_id="TXN-ORD-012", order_id="ORD-2026-012", date=datetime(2026, 3, 6, 11, 10), type=TransactionType.ORDER, description="Order payment received", amount_myr=430.00),
        Transaction(transaction_id="TXN-ORD-013", order_id="ORD-2026-013", date=datetime(2026, 3, 7, 9, 5), type=TransactionType.ORDER, description="Order payment received", amount_myr=112.00),
        Transaction(transaction_id="TXN-ORD-014", order_id="ORD-2026-014", date=datetime(2026, 3, 7, 14, 45), type=TransactionType.ORDER, description="Order payment received", amount_myr=88.50),
        Transaction(transaction_id="TXN-ORD-015", order_id="ORD-2026-015", date=datetime(2026, 3, 8, 10, 30), type=TransactionType.ORDER, description="Order payment received", amount_myr=175.00),
        Transaction(transaction_id="TXN-ORD-016", order_id="ORD-2026-016", date=datetime(2026, 3, 9, 9, 0), type=TransactionType.ORDER, description="Order payment received", amount_myr=62.00),
        Transaction(transaction_id="TXN-ORD-017", order_id="ORD-2026-017", date=datetime(2026, 3, 9, 13, 25), type=TransactionType.ORDER, description="Order payment received", amount_myr=245.00),
        Transaction(transaction_id="TXN-ORD-018", order_id="ORD-2026-018", date=datetime(2026, 3, 10, 11, 55), type=TransactionType.ORDER, description="Order payment received", amount_myr=33.00),
        Transaction(transaction_id="TXN-ORD-019", order_id="ORD-2026-019", date=datetime(2026, 3, 10, 16, 10), type=TransactionType.ORDER, description="Order payment received", amount_myr=199.90),
        Transaction(transaction_id="TXN-ORD-020", order_id="ORD-2026-020", date=datetime(2026, 3, 11, 8, 45), type=TransactionType.ORDER, description="Order payment received", amount_myr=520.00),
        Transaction(transaction_id="TXN-ORD-021", order_id="ORD-2026-021", date=datetime(2026, 3, 11, 14, 0), type=TransactionType.ORDER, description="Order payment received", amount_myr=77.00),
        Transaction(transaction_id="TXN-ORD-022", order_id="ORD-2026-022", date=datetime(2026, 3, 12, 10, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=140.00),
        Transaction(transaction_id="TXN-ORD-023", order_id="ORD-2026-023", date=datetime(2026, 3, 12, 15, 35), type=TransactionType.ORDER, description="Order payment received", amount_myr=93.50),
        Transaction(transaction_id="TXN-ORD-024", order_id="ORD-2026-024", date=datetime(2026, 3, 13, 9, 15), type=TransactionType.ORDER, description="Order payment received", amount_myr=380.00),
        Transaction(transaction_id="TXN-ORD-025", order_id="ORD-2026-025", date=datetime(2026, 3, 13, 12, 50), type=TransactionType.ORDER, description="Order payment received", amount_myr=58.00),
        Transaction(transaction_id="TXN-ORD-026", order_id="ORD-2026-026", date=datetime(2026, 3, 14, 10, 0), type=TransactionType.ORDER, description="Order payment received", amount_myr=167.00),
        Transaction(transaction_id="TXN-ORD-027", order_id="ORD-2026-027", date=datetime(2026, 3, 14, 13, 40), type=TransactionType.ORDER, description="Order payment received", amount_myr=290.00),
        Transaction(transaction_id="TXN-ORD-028", order_id="ORD-2026-028", date=datetime(2026, 3, 15, 9, 30), type=TransactionType.ORDER, description="Order payment received", amount_myr=44.00),
        Transaction(transaction_id="TXN-ORD-029", order_id="ORD-2026-029", date=datetime(2026, 3, 15, 14, 55), type=TransactionType.ORDER, description="Order payment received", amount_myr=105.00),
        Transaction(transaction_id="TXN-ORD-030", order_id="ORD-2026-030", date=datetime(2026, 3, 16, 11, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=225.00),
        Transaction(transaction_id="TXN-ORD-031", order_id="ORD-2026-031", date=datetime(2026, 3, 17, 8, 55), type=TransactionType.ORDER, description="Order payment received", amount_myr=70.00),
        Transaction(transaction_id="TXN-ORD-032", order_id="ORD-2026-032", date=datetime(2026, 3, 17, 12, 30), type=TransactionType.ORDER, description="Order payment received", amount_myr=315.00),
        Transaction(transaction_id="TXN-ORD-033", order_id="ORD-2026-033", date=datetime(2026, 3, 18, 10, 10), type=TransactionType.ORDER, description="Order payment received", amount_myr=82.50),
        Transaction(transaction_id="TXN-ORD-034", order_id="ORD-2026-034", date=datetime(2026, 3, 18, 15, 25), type=TransactionType.ORDER, description="Order payment received", amount_myr=148.00),
        Transaction(transaction_id="TXN-ORD-035", order_id="ORD-2026-035", date=datetime(2026, 3, 19, 9, 40), type=TransactionType.ORDER, description="Order payment received", amount_myr=390.00),
        Transaction(transaction_id="TXN-ORD-036", order_id="ORD-2026-036", date=datetime(2026, 3, 20, 10, 15), type=TransactionType.ORDER, description="Order payment received", amount_myr=115.00),
        Transaction(transaction_id="TXN-ORD-037", order_id="ORD-2026-037", date=datetime(2026, 3, 20, 14, 5), type=TransactionType.ORDER, description="Order payment received", amount_myr=49.00),
        Transaction(transaction_id="TXN-ORD-038", order_id="ORD-2026-038", date=datetime(2026, 3, 21, 9, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=265.00),
        Transaction(transaction_id="TXN-ORD-039", order_id="ORD-2026-039", date=datetime(2026, 3, 21, 13, 50), type=TransactionType.ORDER, description="Order payment received", amount_myr=97.00),
        Transaction(transaction_id="TXN-ORD-040", order_id="ORD-2026-040", date=datetime(2026, 3, 22, 11, 0), type=TransactionType.ORDER, description="Order payment received", amount_myr=178.00),
        Transaction(transaction_id="TXN-ORD-041", order_id="ORD-2026-041", date=datetime(2026, 3, 23, 8, 35), type=TransactionType.ORDER, description="Order payment received", amount_myr=60.00),
        Transaction(transaction_id="TXN-ORD-042", order_id="ORD-2026-042", date=datetime(2026, 3, 23, 14, 20), type=TransactionType.ORDER, description="Order payment received", amount_myr=445.00),
        Transaction(transaction_id="TXN-ORD-043", order_id="ORD-2026-043", date=datetime(2026, 3, 24, 10, 45), type=TransactionType.ORDER, description="Order payment received", amount_myr=130.00),
        Transaction(transaction_id="TXN-ORD-044", order_id="ORD-2026-044", date=datetime(2026, 3, 24, 15, 10), type=TransactionType.ORDER, description="Order payment received", amount_myr=72.50),
        Transaction(transaction_id="TXN-ORD-045", order_id="ORD-2026-045", date=datetime(2026, 3, 25, 9, 55), type=TransactionType.ORDER, description="Order payment received", amount_myr=205.00),
        Transaction(transaction_id="TXN-ORD-046", order_id="ORD-2026-046", date=datetime(2026, 3, 26, 11, 30), type=TransactionType.ORDER, description="Order payment received", amount_myr=88.00),
        Transaction(transaction_id="TXN-ORD-047", order_id="ORD-2026-047", date=datetime(2026, 3, 27, 9, 10), type=TransactionType.ORDER, description="Order payment received", amount_myr=340.00),
        Transaction(transaction_id="TXN-ORD-048", order_id="ORD-2026-048", date=datetime(2026, 3, 28, 10, 25), type=TransactionType.ORDER, description="Order payment received", amount_myr=52.00),
        Transaction(transaction_id="TXN-ORD-049", order_id="ORD-2026-049", date=datetime(2026, 3, 29, 14, 35), type=TransactionType.ORDER, description="Order payment received", amount_myr=163.00),
        Transaction(transaction_id="TXN-ORD-050", order_id="ORD-2026-050", date=datetime(2026, 3, 30, 11, 50), type=TransactionType.ORDER, description="Order payment received", amount_myr=275.00),
    ]

    # --- 5 Refunds ---
    # Refunds 1-4 are partial refunds well below the 20% anomaly threshold.
    # Refund 5 is intentionally large (30.8% of order value) to trigger the anomaly detector.
    refunds = [
        Transaction(transaction_id="TXN-REF-001", order_id="ORD-2026-003", date=datetime(2026, 3, 5, 10, 0), type=TransactionType.REFUND, description="Refund issued - item not received (partial)", amount_myr=-8.00),
        Transaction(transaction_id="TXN-REF-002", order_id="ORD-2026-011", date=datetime(2026, 3, 10, 14, 30), type=TransactionType.REFUND, description="Refund issued - damaged packaging (partial)", amount_myr=-10.00),
        Transaction(transaction_id="TXN-REF-003", order_id="ORD-2026-018", date=datetime(2026, 3, 14, 9, 15), type=TransactionType.REFUND, description="Refund issued - missing accessory (partial)", amount_myr=-5.00),
        Transaction(transaction_id="TXN-REF-004", order_id="ORD-2026-028", date=datetime(2026, 3, 19, 11, 20), type=TransactionType.REFUND, description="Refund issued - buyer cancelled (partial)", amount_myr=-8.00),
        # Large refund — intentional anomaly: 120/390 = 30.8% > 20% threshold
        Transaction(transaction_id="TXN-REF-005", order_id="ORD-2026-035", date=datetime(2026, 3, 25, 16, 0), type=TransactionType.REFUND, description="Refund issued - item quality dispute", amount_myr=-120.00, notes="Large refund relative to order value — triggers anomaly"),
    ]

    # --- Platform Fees (~5% of gross sales = ~MYR 419, applied as weekly batch deductions) ---
    platform_fees = [
        Transaction(transaction_id="TXN-FEE-001", date=datetime(2026, 3, 8, 0, 0), type=TransactionType.PLATFORM_FEE, description="Shopee commission fee (Week 1: Mar 1-7)", amount_myr=-88.15),
        Transaction(transaction_id="TXN-FEE-002", date=datetime(2026, 3, 15, 0, 0), type=TransactionType.PLATFORM_FEE, description="Shopee commission fee (Week 2: Mar 8-14)", amount_myr=-104.70),
        Transaction(transaction_id="TXN-FEE-003", date=datetime(2026, 3, 22, 0, 0), type=TransactionType.PLATFORM_FEE, description="Shopee commission fee (Week 3: Mar 15-21)", amount_myr=-102.60),
        Transaction(transaction_id="TXN-FEE-004", date=datetime(2026, 3, 31, 0, 0), type=TransactionType.PLATFORM_FEE, description="Shopee commission fee (Week 4: Mar 22-30)", amount_myr=-123.84),
    ]

    # --- Shipping Fee Deductions (~2% of gross sales) ---
    shipping_fees = [
        Transaction(transaction_id="TXN-SHIP-001", date=datetime(2026, 3, 8, 0, 0), type=TransactionType.SHIPPING, description="Shipping subsidy deduction (Week 1)", amount_myr=-38.50),
        Transaction(transaction_id="TXN-SHIP-002", date=datetime(2026, 3, 15, 0, 0), type=TransactionType.SHIPPING, description="Shipping subsidy deduction (Week 2)", amount_myr=-42.00),
        Transaction(transaction_id="TXN-SHIP-003", date=datetime(2026, 3, 22, 0, 0), type=TransactionType.SHIPPING, description="Shipping subsidy deduction (Week 3)", amount_myr=-40.00),
        Transaction(transaction_id="TXN-SHIP-004", date=datetime(2026, 3, 31, 0, 0), type=TransactionType.SHIPPING, description="Shipping subsidy deduction (Week 4)", amount_myr=-45.50),
    ]

    # --- Promotional Voucher Deduction (one-off Shopee 3.3 campaign) ---
    vouchers = [
        Transaction(transaction_id="TXN-VCH-001", date=datetime(2026, 3, 11, 0, 0), type=TransactionType.VOUCHER, description="Shopee 3.3 sale campaign voucher deduction", amount_myr=-85.00),
    ]

    # --- Final Settlement Payout ---
    # Gross sales:    8,385.80 MYR
    # Refunds:      -   151.00 MYR → Net sales = 8,234.80 MYR
    # Platform fees: -  419.29 MYR
    # Shipping:      -  166.00 MYR
    # Voucher:       -   85.00 MYR
    # Expected payout = 7,564.51 MYR
    # Actual payout   = 7,549.39 MYR  → discrepancy = -15.12 MYR (-0.20%) ✅ within 2% threshold
    settlement = [
        Transaction(transaction_id="TXN-SET-001", date=datetime(2026, 3, 31, 18, 0), type=TransactionType.SETTLEMENT, description="Shopee monthly settlement payout", amount_myr=7_549.39, notes="Actual payout — small discrepancy within acceptable range"),
    ]

    all_transactions = orders + refunds + platform_fees + shipping_fees + vouchers + settlement
    all_transactions.sort(key=lambda t: t.date)
    return all_transactions


def get_summary() -> dict:
    """Return a quick summary of the mock dataset for logging/debugging."""
    txns = get_mock_transactions()
    return {
        "total_transactions": len(txns),
        "orders": len([t for t in txns if t.type.value == "ORDER"]),
        "refunds": len([t for t in txns if t.type.value == "REFUND"]),
        "platform_fees": len([t for t in txns if t.type.value == "PLATFORM_FEE"]),
        "shipping": len([t for t in txns if t.type.value == "SHIPPING"]),
        "vouchers": len([t for t in txns if t.type.value == "VOUCHER"]),
        "settlements": len([t for t in txns if t.type.value == "SETTLEMENT"]),
        "gross_sales_myr": sum(t.amount_myr for t in txns if t.type.value == "ORDER"),
    }
