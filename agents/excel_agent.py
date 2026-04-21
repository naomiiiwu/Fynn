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

from services.excel import generate


class ExcelAgent:
    """Generates .xlsx report and uploads to Supabase Storage."""

    def run(self, pnl_reports: list[dict], combined: dict) -> dict:
        """
        Args:
            pnl_reports: Per-platform P&L dicts.
            combined:    Company-level P&L dict.

        Returns:
            {
                "url":      str | None,   # public download URL (None if Supabase unavailable)
                "filename": str,
                "bytes":    bytes,        # always present — caller can use directly if needed
            }
        """
        period   = combined.get("period", "report")
        filename = _safe_filename(f"Fynn_{period}.xlsx")

        print(f"  [Excel] Generating workbook: {filename}")
        xlsx_bytes = generate(pnl_reports, combined)
        print(f"  [Excel] Workbook generated ({len(xlsx_bytes):,} bytes)")

        url = _upload_to_supabase(xlsx_bytes, period, filename)

        if url:
            print(f"  [Excel] Uploaded → {url}")
        else:
            # Save locally as fallback
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


def _build_combined_pnl(pnl_reports: list[dict], cost_totals_myr: dict | None = None, currency: str = "SGD") -> dict:
    """Aggregate multiple platform P&Ls into a single combined summary."""
    if not pnl_reports:
        return {}

    base = pnl_reports[0]
    combined = {
        "period":             base["period"],
        "platform":           "Combined",
        "currency":           base.get("currency", "SGD"),
        "generated_at":       base.get("generated_at", ""),
        "exchange_rate_used": base.get("exchange_rate_used", {}),
        "revenue": {
            "gross_sales": sum(p["revenue"]["gross_sales"]  for p in pnl_reports),
            "refunds":     sum(p["revenue"]["refunds"]       for p in pnl_reports),
            "net_revenue": sum(p["revenue"]["net_revenue"]   for p in pnl_reports),
        },
        "local_reference": {
            "currency":        base.get("local_reference", {}).get("currency") or base.get("exchange_rate_used", {}).get("from", "MYR"),
            "gross_sales":     sum(p.get("local_reference", p.get("myr_reference", {})).get("gross_sales", 0)     for p in pnl_reports),
            "net_revenue":     sum(p.get("local_reference", p.get("myr_reference", {})).get("net_revenue", 0)     for p in pnl_reports),
            "expected_payout": sum(p.get("local_reference", p.get("myr_reference", {})).get("expected_payout", 0) for p in pnl_reports),
            "actual_payout":   sum(p.get("local_reference", p.get("myr_reference", {})).get("actual_payout", 0)   for p in pnl_reports),
            "discrepancy":     sum(p.get("local_reference", p.get("myr_reference", {})).get("discrepancy", 0)     for p in pnl_reports),
        },
    }

    extra = cost_totals_myr or {}
    rate  = base.get("exchange_rate_used", {}).get("rate", 1.0)

    def _to_cur(myr: float) -> float:
        return round(myr * rate, 2)

    platform_fees  = sum(p["costs"].get("platform_fees", 0) for p in pnl_reports)
    shipping       = sum(p["costs"].get("shipping", 0)      for p in pnl_reports)
    vouchers       = sum(p["costs"].get("vouchers", 0)      for p in pnl_reports)
    cogs           = _to_cur(extra.get("cogs", 0))
    ads            = _to_cur(extra.get("ads", 0))
    warehouse      = _to_cur(extra.get("warehouse", 0))
    payroll        = _to_cur(extra.get("payroll", 0))
    packaging      = _to_cur(extra.get("packaging", 0))
    other_expense  = _to_cur(extra.get("expense", 0))
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
    combined["platforms_included"] = [p.get("platform") for p in pnl_reports]

    net_revenue = combined["revenue"]["net_revenue"]
    net_profit  = round(net_revenue - total_costs, 2)
    margin      = round(net_profit / net_revenue * 100, 2) if net_revenue else 0.0
    combined["profit"] = {"net_profit": net_profit, "profit_margin_pct": margin}
    return combined
