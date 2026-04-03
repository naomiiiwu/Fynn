"""
WhatsApp delivery service via Twilio.

Sends the formatted P&L summary to the seller's WhatsApp number.
Falls back to console print if credentials are missing.
"""

import os
from typing import Optional


class WhatsAppService:
    """Sends P&L summary messages via Twilio WhatsApp API."""

    def __init__(self) -> None:
        """Initialise Twilio credentials from environment variables."""
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.from_number = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        self.to_number = os.getenv("TWILIO_WHATSAPP_TO", "")

    def _is_configured(self) -> bool:
        """
        Check if all required Twilio credentials are present.

        Returns:
            True if the service is ready to send, False otherwise.
        """
        return bool(self.account_sid and self.auth_token and self.to_number)

    def send(self, message: str) -> bool:
        """
        Send a WhatsApp message via Twilio.

        If Twilio credentials are not configured, prints the message to the
        console as a fallback so the pipeline can still complete.

        Args:
            message: The formatted message string to send.

        Returns:
            True if the message was sent (or printed) successfully, False on error.
        """
        if not self._is_configured():
            print("\n[WhatsApp] Credentials not configured — printing to console instead:\n")
            print("=" * 60)
            print(message)
            print("=" * 60)
            return True  # Graceful fallback counts as success

        try:
            # Import here to avoid crashing if twilio is installed but unused
            from twilio.rest import Client  # type: ignore

            client = Client(self.account_sid, self.auth_token)
            msg = client.messages.create(
                from_=self.from_number,
                to=f"whatsapp:{self.to_number}" if not self.to_number.startswith("whatsapp:") else self.to_number,
                body=message,
            )
            print(f"  [WhatsApp] Message sent successfully. SID: {msg.sid}")
            return True
        except ImportError:
            print("  [WhatsApp] twilio package not available — printing to console.\n")
            print("=" * 60)
            print(message)
            print("=" * 60)
            return True
        except Exception as exc:
            print(f"  [WhatsApp] Send failed: {exc} — printing to console instead.\n")
            print("=" * 60)
            print(message)
            print("=" * 60)
            return False
