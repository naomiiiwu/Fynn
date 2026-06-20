"""
Excel Agent — generates the monthly P&L workbook and uploads it to
Supabase Storage so Twilio can send it as a WhatsApp attachment.

Upload path:  reports/{period}/{filename}
Public URL is returned to the caller (WhatsAppAgent).

Fallback: if Supabase Storage is not configured the bytes are saved
locally to /tmp and the local path is returned instead of a URL.
"""

import os
import re
import tempfile
from datetime import datetime

from services.excel import generate, generate_summary


class ExcelAgent:
    """Generates .xlsx report and uploads to Supabase Storage."""

    def run(self, pnl_reports: list[dict], combined: dict) -> dict:
        """
        Args:
            pnl_reports: Per-(platform × period) P&L dicts.
            combined:    Combined P&L dict (output of _build_combined_pnl).

        Returns:
            {
                "url":      str | None,   # public download URL (None if Supabase unavailable)
                "filename": str,
                "bytes":    bytes,        # always present
            }
        """
        period   = combined.get("period", "report")
        breakdown = combined.get("monthly_breakdown", [])
        is_multi_period = len({r["period"] for r in breakdown}) > 1

        if is_multi_period:
            # Summary-only Excel: one row per (platform × period) — stays small forever
            filename   = _safe_filename(f"Fynn_Summary_{period}.xlsx")
            print(f"  [Excel] Generating consolidated summary workbook: {filename}")
            xlsx_bytes = generate_summary(breakdown, combined)
        else:
            # Single period: full detail workbook (existing behaviour)
            filename   = _safe_filename(f"Fynn_{period}.xlsx")
            print(f"  [Excel] Generating detail workbook: {filename}")
            xlsx_bytes = generate(pnl_reports, combined)

        print(f"  [Excel] Workbook generated ({len(xlsx_bytes):,} bytes)")
        url = _upload_to_supabase(xlsx_bytes, period, filename)

        if url:
            print(f"  [Excel] Uploaded → {url}")
        else:
            path = os.path.join(tempfile.gettempdir(), filename)
            with open(path, "wb") as f:
                f.write(xlsx_bytes)
            print(f"  [Excel] Supabase unavailable — saved locally: {path}")

        return {"url": url, "filename": filename, "bytes": xlsx_bytes}


# ── Supabase Storage helpers ───────────────────────────────────────────────────

_BUCKET = "reports"


def _upload_to_supabase(data: bytes, period: str, filename: str) -> str | None:
    """Upload bytes to Supabase Storage and return the public URL, or None on failure."""
    try:
        from services.database import _get_client
        client = _get_client()
        if client is None:
            return None

        # Ensure the bucket exists (creates it if not — safe to call every time)
        try:
            client.storage.create_bucket(_BUCKET, options={"public": True})
            print(f"  [Excel] Created Supabase Storage bucket '{_BUCKET}'.")
        except Exception:
            pass  # bucket already exists — that's fine

        folder       = re.sub(r"[^\w\-]", "_", period)
        path         = f"{folder}/{filename}"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        client.storage.from_(_BUCKET).upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        url = client.storage.from_(_BUCKET).get_public_url(path)
        return url
    except Exception as exc:
        print(f"  [Excel] Supabase upload failed: {exc}")
        return None


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)


def _build_combined_pnl(pnl_reports: list[dict], period_label: str = "", currency: str = "SGD") -> dict:
    """
    Aggregate per-(platform × period) P&L dicts into a single combined summary.

    Costs are already converted and baked into each report by PnLAgent,
    so this function purely sums — no extra currency conversion needed.
    """
    if not pnl_reports:
        return {}

    base = pnl_reports[0]
    label = period_label or base["period"]

    combined = {
        "period":             label,
        "platform":           "Combined",
        "currency":           base.get("currency", currency),
        "generated_at":       base.get("generated_at", ""),
        "exchange_rate_used": base.get("exchange_rate_used", {}),
        "revenue": {
            "gross_sales": round(sum(p["revenue"]["gross_sales"]  for p in pnl_reports), 2),
            "refunds":     round(sum(p["revenue"]["refunds"]       for p in pnl_reports), 2),
            "net_revenue": round(sum(p["revenue"]["net_revenue"]   for p in pnl_reports), 2),
        },
        "local_reference": {
            "currency":        base.get("local_reference", {}).get("currency") or base.get("exchange_rate_used", {}).get("from", "MYR"),
            "gross_sales":     sum(p.get("local_reference", {}).get("gross_sales", 0)     for p in pnl_reports),
            "net_revenue":     sum(p.get("local_reference", {}).get("net_revenue", 0)     for p in pnl_reports),
            "expected_payout": sum(p.get("local_reference", {}).get("expected_payout", 0) for p in pnl_reports),
            "actual_payout":   sum(p.get("local_reference", {}).get("actual_payout", 0)   for p in pnl_reports),
            "discrepancy":     sum(p.get("local_reference", {}).get("discrepancy", 0)     for p in pnl_reports),
        },
    }

    def _sum(key: str) -> float:
        return round(sum(p["costs"].get(key, 0) for p in pnl_reports), 2)

    platform_fees  = _sum("platform_fees")
    shipping       = _sum("shipping")
    vouchers       = _sum("vouchers")
    cogs           = _sum("cogs")
    ads            = _sum("ads")
    warehouse      = _sum("warehouse")
    payroll        = _sum("payroll")
    packaging      = _sum("packaging")
    other_expense  = _sum("other_expense")
    total_platform = round(platform_fees + shipping + vouchers, 2)
    total_business = round(cogs + ads + warehouse + payroll + packaging + other_expense, 2)
    total_costs    = round(total_platform + total_business, 2)

    combined["costs"] = {
        "platform_fees": platform_fees, "shipping": shipping, "vouchers": vouchers,
        "cogs": cogs, "ads": ads, "warehouse": warehouse,
        "payroll": payroll, "packaging": packaging, "other_expense": other_expense,
        "total_platform_costs": total_platform,
        "total_business_costs": total_business,
        "total_costs":          total_costs,
    }
    combined["anomalies"]          = [a for p in pnl_reports for a in p.get("anomalies", [])]
    combined["order_count"]        = sum(p.get("order_count", 0)  for p in pnl_reports)
    combined["refund_count"]       = sum(p.get("refund_count", 0) for p in pnl_reports)
    combined["platforms_included"] = list({p.get("platform") for p in pnl_reports})

    net_revenue = combined["revenue"]["net_revenue"]
    net_profit  = round(net_revenue - total_costs, 2)
    margin      = round(net_profit / net_revenue * 100, 2) if net_revenue else 0.0
    combined["profit"] = {"net_profit": net_profit, "profit_margin_pct": margin}

    # Per-(platform × period) breakdown — used by summary Excel and WhatsApp
    combined["monthly_breakdown"] = [
        {
            "period":       p["period"],
            "platform":     p["platform"],
            "net_revenue":  p["revenue"]["net_revenue"],
            "total_costs":  p["costs"]["total_costs"],
            "net_profit":   p["profit"]["net_profit"],
            "margin_pct":   p["profit"]["profit_margin_pct"],
            "order_count":  p.get("order_count", 0),
        }
        for p in pnl_reports
    ]

    return combined
