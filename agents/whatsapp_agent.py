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
        combined: dict | None = None,
    ) -> bool:
        if not pnl_reports:
            return False

        message = self._format(pnl_reports, profile, combined)
        wa = WhatsAppService(to=to)
        ok = wa.send(message)
        print(f"  [WhatsApp] Message sent to {to or 'default'}: {'ok' if ok else 'failed'}")
        return ok

    def _format(self, pnl_reports: list[dict], profile: UserProfile, combined: dict | None = None) -> str:
        currency = profile.currency
        name     = profile.name

        spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
        sheets_line = (
            f"📄 Full breakdown: https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            if spreadsheet_id else "📄 Full breakdown saved to Google Sheets"
        )

        # Use the combined P&L (with business costs) if available, otherwise single platform
        pnl = combined or pnl_reports[0]
        return format_whatsapp_message(pnl, seller_name=name, pnl_reports=pnl_reports)
