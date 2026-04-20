"""WhatsApp Agent — formats and sends the P&L summary + Excel attachment to the seller."""

from models.user_profile import UserProfile
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message


class WhatsAppAgent:
    """Formats and sends the WhatsApp message with an optional Excel attachment."""

    def run(
        self,
        pnl_reports: list[dict],
        profile: UserProfile,
        to: str | None = None,
        combined: dict | None = None,
        excel_url: str | None = None,
    ) -> bool:
        if not pnl_reports:
            return False

        message = self._format(pnl_reports, profile, combined, excel_url=excel_url)
        wa = WhatsAppService(to=to)
        ok = wa.send(message, media_url=excel_url)
        attachment_note = f" + Excel attachment" if excel_url else ""
        print(f"  [WhatsApp] Message{attachment_note} sent to {to or 'default'}: {'ok' if ok else 'failed'}")
        return ok

    def _format(
        self,
        pnl_reports: list[dict],
        profile: UserProfile,
        combined: dict | None = None,
        excel_url: str | None = None,
    ) -> str:
        pnl     = combined or pnl_reports[0]
        message = format_whatsapp_message(pnl, seller_name=profile.name, pnl_reports=pnl_reports)
        if excel_url:
            message += "\n📎 Full P&L attached as Excel file."
        return message
