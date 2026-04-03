"""
Report formatting helpers for Fynn P&L generation.

Produces the canonical P&L dict structure consumed by
Google Sheets output and WhatsApp delivery.
"""

from datetime import datetime
from typing import List

from models.transaction import Anomaly, ReconciliationResult, Transaction, TransactionType


def generate_pnl(
    transactions: List[Transaction],
    reconciliation: ReconciliationResult,
    anomalies: List[Anomaly],
    sgd_conversion: dict,
    period: str = "March 2026",
    platform: str = "Shopee MY",
) -> dict:
    """
    Build the canonical P&L report dictionary in SGD.

    Args:
        transactions:    All categorized transactions for the period.
        reconciliation:  Pre-computed reconciliation result (in MYR).
        anomalies:       List of detected anomalies.
        sgd_conversion:  Output of CurrencyConverter.myr_to_sgd() for net profit.
        period:          Human-readable period label (e.g. "March 2026").
        platform:        Platform label (e.g. "Shopee MY").

    Returns:
        P&L dict with all amounts in SGD.
    """
    print("\n[Formatter] Generating P&L report...")

    rate = sgd_conversion["exchange_rate"]

    def to_sgd(myr_amount: float) -> float:
        """Convert a MYR amount to SGD using the cached exchange rate."""
        return round(myr_amount * rate, 2)

    # Revenue
    gross_sales_sgd = to_sgd(reconciliation.gross_sales_myr)
    refunds_sgd = to_sgd(reconciliation.total_refunds_myr)
    net_revenue_sgd = round(gross_sales_sgd - refunds_sgd, 2)

    # Costs
    platform_fees_sgd = to_sgd(reconciliation.total_platform_fees_myr)
    shipping_sgd = to_sgd(reconciliation.total_shipping_myr)
    vouchers_sgd = to_sgd(reconciliation.total_vouchers_myr)
    total_costs_sgd = round(platform_fees_sgd + shipping_sgd + vouchers_sgd, 2)

    # Profit
    net_profit_sgd = round(net_revenue_sgd - total_costs_sgd, 2)
    profit_margin_pct = round(net_profit_sgd / net_revenue_sgd * 100, 2) if net_revenue_sgd != 0 else 0.0

    # Count orders and refunds
    order_count = sum(1 for t in transactions if t.type == TransactionType.ORDER)
    refund_count = sum(1 for t in transactions if t.type == TransactionType.REFUND)

    pnl = {
        "period": period,
        "platform": platform,
        "currency": "SGD",
        "exchange_rate_used": {"from": "MYR", "to": "SGD", "rate": rate, "source": sgd_conversion["source"]},
        "revenue": {
            "gross_sales": gross_sales_sgd,
            "refunds": refunds_sgd,
            "net_revenue": net_revenue_sgd,
        },
        "costs": {
            "platform_fees": platform_fees_sgd,
            "shipping": shipping_sgd,
            "vouchers": vouchers_sgd,
            "total_costs": total_costs_sgd,
        },
        "profit": {
            "net_profit": net_profit_sgd,
            "profit_margin_pct": profit_margin_pct,
        },
        "anomalies": [a.model_dump() for a in anomalies],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "order_count": order_count,
        "refund_count": refund_count,
        # Also include MYR figures for reference
        "myr_reference": {
            "gross_sales": reconciliation.gross_sales_myr,
            "net_revenue": round(reconciliation.gross_sales_myr - reconciliation.total_refunds_myr, 2),
            "expected_payout": reconciliation.expected_payout_myr,
            "actual_payout": reconciliation.actual_payout_myr,
            "discrepancy": reconciliation.discrepancy_myr,
        },
    }

    print(f"  Period:         {pnl['period']}")
    print(f"  Net Revenue:    SGD {net_revenue_sgd:>10,.2f}")
    print(f"  Total Costs:    SGD {total_costs_sgd:>10,.2f}")
    print(f"  Net Profit:     SGD {net_profit_sgd:>10,.2f}")
    print(f"  Profit Margin:  {profit_margin_pct:.2f}%")
    print(f"  Anomalies:      {len(anomalies)}")

    return pnl


def format_whatsapp_message(pnl: dict, seller_name: str = "Seller") -> str:
    """
    Format the P&L data into a WhatsApp-ready summary message.

    Args:
        pnl:         The P&L dict produced by generate_pnl().
        seller_name: The seller's name for personalisation.

    Returns:
        Formatted multi-line string ready to send via Twilio.
    """
    import os
    period = pnl["period"]
    net_revenue = pnl["revenue"]["net_revenue"]
    order_count = pnl["order_count"]
    refund_count = pnl["refund_count"]
    profit_margin = pnl["profit"]["profit_margin_pct"]
    anomalies = pnl.get("anomalies", [])

    net_profit = pnl["profit"]["net_profit"]
    gross_sales = pnl["revenue"]["gross_sales"]

    # Build Google Sheets link
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if spreadsheet_id:
        sheets_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        sheets_line = f"📄 Full P&L: {sheets_link}"
    else:
        sheets_line = "📄 Full P&L saved to pnl_report.json"

    if anomalies:
        anomaly_lines = "\n".join(f"⚠️ {a['description']}" for a in anomalies)
        anomaly_block = f"*Heads up:*\n{anomaly_lines}"
    else:
        anomaly_block = "✅ All clear — no anomalies detected."

    message = (
        f"Hey {seller_name}! 👋 Your *{period}* books are done.\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Shopee MY — {period}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Gross Sales:     SGD {gross_sales:>8,.2f}\n"
        f"↩️  Refunds:         {refund_count} order{'s' if refund_count != 1 else ''}\n"
        f"💵 Net Revenue:     SGD {net_revenue:>8,.2f}\n"
        f"📈 Net Profit:      SGD {net_profit:>8,.2f}\n"
        f"📉 Profit Margin:   {profit_margin}%\n"
        f"📦 Orders:          {order_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"{anomaly_block}\n"
        f"\n"
        f"{sheets_line}\n"
        f"— Fynn 🤖"
    )
    return message


def save_pnl_json(pnl: dict, filepath: str = "pnl_report.json") -> None:
    """
    Save the P&L dict to a local JSON file (fallback when Sheets is unavailable).

    Args:
        pnl:      The P&L dict to save.
        filepath: Output file path.
    """
    import json

    with open(filepath, "w") as f:
        json.dump(pnl, f, indent=2)
    print(f"  [Formatter] P&L saved to {filepath}")
