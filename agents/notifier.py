"""Outbound WhatsApp notifications.

The webhook answers Twilio synchronously with TwiML; anything that has to be
sent *after* the reply — a digest built in a background task, a confirmation
that a file was ingested — goes out through here on the Twilio REST API.
"""
from __future__ import annotations

from typing import Optional

from services.whatsapp import WhatsAppService


class Notifier:
    """Sends a rendered message to one accountant's WhatsApp number."""

    def send(self, to: str, message: str, media_url: Optional[str] = None) -> bool:
        ok = WhatsAppService(to=to).send(message, media_url=media_url)
        print(f"  [Notify] Message to {to}: {'ok' if ok else 'failed'}")
        return ok

    def digest(self, to: str, cycle) -> bool:
        """Send the month-end review message for a cycle."""
        return self.send(to, cycle.whatsapp_digest())
