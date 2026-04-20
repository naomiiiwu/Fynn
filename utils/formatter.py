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
    additional_costs_myr: dict = None,
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

    # Platform-side costs (from transaction data)
    platform_fees_sgd = to_sgd(reconciliation.total_platform_fees_myr)
    shipping_sgd      = to_sgd(reconciliation.total_shipping_myr)
    vouchers_sgd      = to_sgd(reconciliation.total_vouchers_myr)

    # Business costs (from uploaded cost CSVs)
    extra = additional_costs_myr or {}
    cogs_sgd      = to_sgd(extra.get("cogs", 0.0))
    ads_sgd       = to_sgd(extra.get("ads", 0.0))
    warehouse_sgd = to_sgd(extra.get("warehouse", 0.0))
    payroll_sgd   = to_sgd(extra.get("payroll", 0.0))
    packaging_sgd = to_sgd(extra.get("packaging", 0.0))
    expense_sgd   = to_sgd(extra.get("expense", 0.0))

    total_platform_costs_sgd = round(platform_fees_sgd + shipping_sgd + vouchers_sgd, 2)
    total_business_costs_sgd = round(cogs_sgd + ads_sgd + warehouse_sgd + payroll_sgd + packaging_sgd + expense_sgd, 2)
    total_costs_sgd          = round(total_platform_costs_sgd + total_business_costs_sgd, 2)

    # Profit
    net_profit_sgd     = round(net_revenue_sgd - total_costs_sgd, 2)
    profit_margin_pct  = round(net_profit_sgd / net_revenue_sgd * 100, 2) if net_revenue_sgd != 0 else 0.0

    # Count orders and refunds
    order_count  = sum(1 for t in transactions if t.type == TransactionType.ORDER)
    refund_count = sum(1 for t in transactions if t.type == TransactionType.REFUND)

    pnl = {
        "period":   period,
        "platform": platform,
        "currency": "SGD",
        "exchange_rate_used": {"from": "MYR", "to": "SGD", "rate": rate, "source": sgd_conversion["source"]},
        "revenue": {
            "gross_sales": gross_sales_sgd,
            "refunds":     refunds_sgd,
            "net_revenue": net_revenue_sgd,
        },
        "costs": {
            # Platform-side (from Shopee/Lazada CSV)
            "platform_fees": platform_fees_sgd,
            "shipping":      shipping_sgd,
            "vouchers":      vouchers_sgd,
            # Business costs (from uploaded cost CSVs)
            "cogs":          cogs_sgd,
            "ads":           ads_sgd,
            "warehouse":     warehouse_sgd,
            "payroll":       payroll_sgd,
            "packaging":     packaging_sgd,
            "other_expense": expense_sgd,
            # Totals
            "total_platform_costs": total_platform_costs_sgd,
            "total_business_costs": total_business_costs_sgd,
            "total_costs":          total_costs_sgd,
        },
        "profit": {
            "net_profit":        net_profit_sgd,
            "profit_margin_pct": profit_margin_pct,
        },
        "anomalies":    [a.model_dump() for a in anomalies],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "order_count":  order_count,
        "refund_count": refund_count,
        "myr_reference": {
            "gross_sales":      reconciliation.gross_sales_myr,
            "net_revenue":      round(reconciliation.gross_sales_myr - reconciliation.total_refunds_myr, 2),
            "expected_payout":  reconciliation.expected_payout_myr,
            "actual_payout":    reconciliation.actual_payout_myr,
            "discrepancy":      reconciliation.discrepancy_myr,
        },
    }

    print(f"  Period:              {pnl['period']}")
    print(f"  Net Revenue:    SGD {net_revenue_sgd:>10,.2f}")
    print(f"  Platform Costs: SGD {total_platform_costs_sgd:>10,.2f}")
    print(f"  Business Costs: SGD {total_business_costs_sgd:>10,.2f}")
    print(f"  Total Costs:    SGD {total_costs_sgd:>10,.2f}")
    print(f"  Net Profit:     SGD {net_profit_sgd:>10,.2f}")
    print(f"  Profit Margin:  {profit_margin_pct:.2f}%")
    print(f"  Anomalies:      {len(anomalies)}")

    return pnl


def format_whatsapp_message(pnl: dict, seller_name: str = "Seller", pnl_reports: list = None) -> str:
    """
    Format the P&L data into a WhatsApp-ready summary message.

    Args:
        pnl:         The P&L dict produced by generate_pnl().
        seller_name: The seller's name for personalisation.

    Returns:
        Formatted multi-line string ready to send via Twilio.
    """
    period = pnl["period"]
    net_revenue = pnl["revenue"]["net_revenue"]
    order_count = pnl["order_count"]
    refund_count = pnl["refund_count"]
    profit_margin = pnl["profit"]["profit_margin_pct"]
    anomalies = pnl.get("anomalies", [])

    net_profit  = pnl["profit"]["net_profit"]
    gross_sales = pnl["revenue"]["gross_sales"]
    costs       = pnl.get("costs", {})
    currency    = pnl.get("currency", "SGD")

    # Anomalies — cap at 2 to stay under 1600 chars; full list is in Sheets
    all_anomalies = anomalies
    if pnl_reports:
        all_anomalies = [a for p in pnl_reports for a in p.get("anomalies", [])]
    if all_anomalies:
        shown = all_anomalies[:2]
        extra = len(all_anomalies) - len(shown)
        anomaly_lines = "\n".join(f"⚠️ {a['description'][:80]}" for a in shown)
        anomaly_block = f"*Heads up:*\n{anomaly_lines}"
        if extra:
            anomaly_block += f"\n_...and {extra} more — see full report in Sheets_"
    else:
        anomaly_block = "✅ All clear — no anomalies detected."

    platform_label = pnl.get("platform", "Platform")
    # For multi-platform, add a compact per-platform revenue line
    platform_lines = ""
    if pnl_reports and len(pnl_reports) > 1:
        lines = []
        for p in pnl_reports:
            label = p.get("platform", "?")
            rev   = p["revenue"]["net_revenue"]
            lines.append(f"   {label}: {currency} {rev:,.2f}")
        platform_lines = "\n".join(lines) + "\n"

    # Build cost breakdown — only show lines that have a non-zero value
    def cost_line(label: str, key: str) -> str:
        val = costs.get(key, 0.0)
        return f"   {label}  -{currency} {val:>8,.2f}\n" if val else ""

    cost_block = (
        f"*Platform costs:*\n"
        f"{cost_line('Commission & fees', 'platform_fees')}"
        f"{cost_line('Shipping', 'shipping')}"
        f"{cost_line('Vouchers', 'vouchers')}"
    )
    business_costs = "".join(filter(None, [
        cost_line("COGS (supplier)", "cogs"),
        cost_line("Ads spend", "ads"),
        cost_line("Warehouse / 3PL", "warehouse"),
        cost_line("Payroll", "payroll"),
        cost_line("Packaging", "packaging"),
        cost_line("Other expenses", "other_expense"),
    ]))
    if business_costs:
        cost_block += f"*Business costs:*\n{business_costs}"

    message = (
        f"Hey {seller_name}! 👋 Your *{period}* books are done.\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *{platform_label} — {period}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Gross Sales:   {currency} {gross_sales:,.2f}\n"
        f"↩️  Refunds:       {refund_count} order{'s' if refund_count != 1 else ''}\n"
        f"💵 Net Revenue:   {currency} {net_revenue:,.2f}\n"
        f"{platform_lines}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{cost_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Net Profit:    {currency} {net_profit:,.2f}\n"
        f"📉 Margin:        {profit_margin}%\n"
        f"📦 Orders:        {order_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"\n{anomaly_block}\n"
        f"\n📎 Full breakdown attached as Excel file.\n"
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
