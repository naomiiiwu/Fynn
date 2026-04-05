"""Sheets Agent — writes P&L reports to Google Sheets (one tab per platform + Summary)."""

from services.sheets import SheetsService
from utils.formatter import save_pnl_json


class SheetsAgent:
    """Writes one tab per platform plus a combined Summary tab."""

    def run(self, pnl_reports: list[dict]) -> dict:
        sheets = SheetsService()

        if len(pnl_reports) == 1:
            # Single platform — write as before
            ok = sheets.write_pnl(pnl_reports[0])
            if not ok:
                save_pnl_json(pnl_reports[0])
            return {"success": ok, "tabs_written": [pnl_reports[0].get("platform", "Sheet")]}

        # Multi-platform — write each tab then summary
        tabs_written = []
        all_ok = True
        for pnl in pnl_reports:
            ok = sheets.write_pnl(pnl)
            if ok:
                tabs_written.append(pnl.get("platform", "Sheet"))
            else:
                save_pnl_json(pnl)
                all_ok = False

        # Write combined summary tab
        if len(pnl_reports) > 1:
            combined = _build_combined_pnl(pnl_reports)
            sheets.write_pnl(combined)
            tabs_written.append("Combined")

        print(f"  [Sheets] Tabs written: {tabs_written}")
        return {"success": all_ok, "tabs_written": tabs_written}


def _build_combined_pnl(pnl_reports: list[dict]) -> dict:
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
        "costs": {
            "platform_fees": sum(p["costs"]["platform_fees"] for p in pnl_reports),
            "shipping":      sum(p["costs"]["shipping"]      for p in pnl_reports),
            "vouchers":      sum(p["costs"]["vouchers"]      for p in pnl_reports),
            "total_costs":   sum(p["costs"]["total_costs"]   for p in pnl_reports),
        },
        "profit": {},
        "anomalies":   [a for p in pnl_reports for a in p.get("anomalies", [])],
        "order_count": sum(p.get("order_count", 0) for p in pnl_reports),
        "refund_count": sum(p.get("refund_count", 0) for p in pnl_reports),
        "generated_at": base.get("generated_at", ""),
        "platforms_included": [p.get("platform") for p in pnl_reports],
    }
    net_revenue = combined["revenue"]["net_revenue"]
    net_profit  = round(net_revenue - combined["costs"]["total_costs"], 2)
    margin      = round(net_profit / net_revenue * 100, 2) if net_revenue else 0.0
    combined["profit"] = {"net_profit": net_profit, "profit_margin_pct": margin}
    return combined
