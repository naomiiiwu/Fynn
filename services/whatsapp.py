"""
WhatsApp delivery service via Twilio.

Sends the formatted P&L summary to the seller's WhatsApp number.
Falls back to console print if credentials are missing.
"""

import os
from typing import Optional


class WhatsAppService:
    """Sends P&L summary messages via Twilio WhatsApp API."""

    def __init__(self, to: Optional[str] = None) -> None:
        """Initialise Twilio credentials from environment variables."""
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.from_number = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip()
        # Prefer the explicit `to` argument (per-user), fall back to env default
        self.to_number = to or os.getenv("TWILIO_WHATSAPP_TO", "").strip()

        if self.from_number and not self.from_number.startswith("whatsapp:"):
            self.from_number = f"whatsapp:{self.from_number}"
        if self.to_number and not self.to_number.startswith("whatsapp:"):
            self.to_number = f"whatsapp:{self.to_number}"

    def _is_configured(self) -> bool:
        """
        Check if all required Twilio credentials are present.

        Returns:
            True if the service is ready to send, False otherwise.
        """
        return bool(self.account_sid and self.auth_token and self.to_number)

    def send(self, message: str, media_url: Optional[str] = None) -> bool:
        """
        Send a WhatsApp message via Twilio, optionally with a file attachment.

        Args:
            message:   The formatted message string to send.
            media_url: Publicly accessible URL of a file to attach (e.g. .xlsx).

        Returns:
            True if the message was sent (or printed) successfully, False on error.
        """
        if not self._is_configured():
            print("\n[WhatsApp] Credentials not configured — printing to console instead:\n")
            print("=" * 60)
            print(message)
            if media_url:
                print(f"[Attachment] {media_url}")
            print("=" * 60)
            return True  # Graceful fallback counts as success

        try:
            from twilio.rest import Client  # type: ignore
        except ImportError:
            print("  [WhatsApp] twilio package not available — printing to console.\n")
            print("=" * 60)
            print(message)
            if media_url:
                print(f"[Attachment] {media_url}")
            print("=" * 60)
            return True

        client = Client(self.account_sid, self.auth_token)

        # Try with attachment first; fall back to text-only with URL in body
        for attempt, kwargs in enumerate([
            dict(from_=self.from_number, to=self.to_number, body=message,
                 **{"media_url": [media_url]} if media_url else {}),
            dict(from_=self.from_number, to=self.to_number,
                 body=message + (f"\n\n📎 Download report: {media_url}" if media_url else "")),
        ]):
            try:
                msg = client.messages.create(**kwargs)
                if attempt == 0:
                    print(f"  [WhatsApp] Sent with attachment. SID: {msg.sid}")
                else:
                    print(f"  [WhatsApp] Sent without attachment (fallback). SID: {msg.sid}")
                return True
            except Exception as exc:
                print(f"  [WhatsApp] Attempt {attempt + 1} failed: {exc}")
                if attempt == 1:
                    print("  [WhatsApp] Both attempts failed — printing to console.")
                    print("=" * 60)
                    print(message)
                    print("=" * 60)
                    return False
