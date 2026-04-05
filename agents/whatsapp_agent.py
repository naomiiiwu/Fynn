"""WhatsApp Agent — formats and sends the P&L summary to the seller."""

import os

from models.user_profile import UserProfile
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message


class WhatsAppAgent:
    """Formats and sends the WhatsApp message. Handles single and multi-platform."""

    def run(
        self,
        pnl_reports: list[dict],
        profile: UserProfile,
        to: str | None = None,
    ) -> bool:
        if not pnl_reports:
            return False

        message = self._format(pnl_reports, profile)
        wa = WhatsAppService(to=to)
        ok = wa.send(message)
        print(f"  [WhatsApp] Message sent to {to or 'default'}: {'ok' if ok else 'failed'}")
        return ok

    def _format(self, pnl_reports: list[dict], profile: UserProfile) -> str:
        currency = profile.currency
        name     = profile.name

        spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
        sheets_line = (
            f"📄 Full P&L: https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            if spreadsheet_id else "📄 Full P&L saved to Google Sheets"
        )

        if len(pnl_reports) == 1:
            # Single platform — use existing formatter
            pnl = pnl_reports[0]
            # Update currency label dynamically
            msg = format_whatsapp_message(pnl, seller_name=name)
            return msg

        # Multi-platform — combined header + per-platform breakdown
        period = pnl_reports[0]["period"]
        total_gross   = sum(p["revenue"]["gross_sales"]  for p in pnl_reports)
        total_revenue = sum(p["revenue"]["net_revenue"]  for p in pnl_reports)
        total_profit  = sum(p["profit"]["net_profit"]    for p in pnl_reports)
        total_orders  = sum(p.get("order_count", 0)      for p in pnl_reports)
        margin = round(total_profit / total_revenue * 100, 2) if total_revenue else 0

        lines = [
            f"Hey {name}! 👋 Your *{period}* books are done.\n",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🌐 *Combined — {period}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"💰 Gross Sales:   {currency} {total_gross:>8,.2f}",
            f"💵 Net Revenue:   {currency} {total_revenue:>8,.2f}",
            f"📈 Net Profit:    {currency} {total_profit:>8,.2f}",
            f"📉 Margin:        {margin}%",
            f"📦 Total Orders:  {total_orders}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        # Per-platform breakdown
        for pnl in pnl_reports:
            platform_label = pnl.get("platform", "Platform")
            p_profit = pnl["profit"]["net_profit"]
            p_orders = pnl.get("order_count", 0)
            narrative = pnl.get("narrative", "")
            lines.append(f"\n📦 *{platform_label}*")
            lines.append(f"   Net Profit: {currency} {p_profit:,.2f}  ({p_orders} orders)")
            if narrative:
                lines.append(f"   _{narrative}_")

        # Anomalies across all platforms
        all_anomalies = [a for p in pnl_reports for a in p.get("anomalies", [])]
        if all_anomalies:
            lines.append(f"\n*Heads up:*")
            for a in all_anomalies:
                lines.append(f"⚠️ {a['description']}")
        else:
            lines.append("\n✅ All clear — no anomalies detected.")

        lines.append(f"\n{sheets_line}")
        lines.append("— Fynn 🤖")
        return "\n".join(lines)
