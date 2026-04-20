"""Sheets Agent — writes P&L reports to Google Sheets (one tab per platform + Summary)."""

from services.sheets import SheetsService
from utils.formatter import save_pnl_json


class SheetsAgent:
    """Writes one tab per platform plus a combined Summary tab."""

    def run(self, pnl_reports: list[dict], combined: dict | None = None) -> dict:
        sheets = SheetsService()
        tabs_written = []
        all_ok = True

        if len(pnl_reports) == 1:
            pnl = combined or pnl_reports[0]
            ok = sheets.write_pnl(pnl)
            if not ok:
                save_pnl_json(pnl)
                all_ok = False
            else:
                tabs_written.append(pnl.get("platform", "Sheet"))
        else:
            # Write each platform tab (platform costs only)
            for pnl in pnl_reports:
                ok = sheets.write_pnl(pnl)
                if ok:
                    tabs_written.append(pnl.get("platform", "Sheet"))
                else:
                    save_pnl_json(pnl)
                    all_ok = False

            # Write combined tab (with business costs)
            if combined:
                sheets.write_pnl(combined)
                tabs_written.append("Combined")

        print(f"  [Sheets] Tabs written: {tabs_written}")
        return {"success": all_ok, "tabs_written": tabs_written}


def _build_combined_pnl(pnl_reports: list[dict], cost_totals_myr: dict | None = None, currency: str = "SGD") -> dict:
    """Aggregate multiple platform P&Ls into a combined summary."""
    if not pnl_reports:
        return {}

    base = pnl_reports[0]
    combined = {
        "period":   base["period"],
        "platform": "Combined",
        "currency": base.get("currency", "SGD"),
        "revenue": {
            "gross_sales": sum(p["revenue"]["gross_sales"]  for p in pnl_reports),
            "refunds":     sum(p["revenue"]["refunds"]       for p in pnl_reports),
            "net_revenue": sum(p["revenue"]["net_revenue"]   for p in pnl_reports),
        },
    }

    # Convert business costs from MYR to the report currency
    extra = cost_totals_myr or {}
    rate = base.get("exchange_rate_used", {}).get("rate", 1.0)
    def _to_cur(myr: float) -> float:
        return round(myr * rate, 2)

    platform_fees    = sum(p["costs"].get("platform_fees", 0) for p in pnl_reports)
    shipping         = sum(p["costs"].get("shipping", 0)      for p in pnl_reports)
    vouchers         = sum(p["costs"].get("vouchers", 0)      for p in pnl_reports)
    cogs             = _to_cur(extra.get("cogs", 0))
    ads              = _to_cur(extra.get("ads", 0))
    warehouse        = _to_cur(extra.get("warehouse", 0))
    payroll          = _to_cur(extra.get("payroll", 0))
    packaging        = _to_cur(extra.get("packaging", 0))
    other_expense    = _to_cur(extra.get("expense", 0))
    total_platform   = round(platform_fees + shipping + vouchers, 2)
    total_business   = round(cogs + ads + warehouse + payroll + packaging + other_expense, 2)
    total_costs      = round(total_platform + total_business, 2)

    combined["costs"] = {
        "platform_fees": platform_fees, "shipping": shipping, "vouchers": vouchers,
        "cogs": cogs, "ads": ads, "warehouse": warehouse,
        "payroll": payroll, "packaging": packaging, "other_expense": other_expense,
        "total_platform_costs": total_platform,
        "total_business_costs": total_business,
        "total_costs": total_costs,
    }
    combined["anomalies"]    = [a for p in pnl_reports for a in p.get("anomalies", [])]
    combined["order_count"]  = sum(p.get("order_count", 0)  for p in pnl_reports)
    combined["refund_count"] = sum(p.get("refund_count", 0) for p in pnl_reports)
    combined["generated_at"] = base.get("generated_at", "")
    combined["platforms_included"] = [p.get("platform") for p in pnl_reports]

    net_revenue = combined["revenue"]["net_revenue"]
    net_profit  = round(net_revenue - total_costs, 2)
    margin      = round(net_profit / net_revenue * 100, 2) if net_revenue else 0.0
    combined["profit"] = {"net_profit": net_profit, "profit_margin_pct": margin}
    return combined
