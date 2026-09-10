"""Outbound WhatsApp notifications.

The webhook answers Twilio synchronously with TwiML; anything that has to be
sent *after* the reply — a digest built in a background task, a confirmation
that a file was ingested — goes out through here on the Twilio REST API.

What gets sent is a one-line summary plus a tokenised link. The reasoning and
the approve buttons live on the page, not in the thread: an accountant reviewing
figures wanted a report, and a phone is a bad place to read a journal.
"""
from __future__ import annotations

from typing import Optional

from services.whatsapp import issue_link, send


class Notifier:
    """Sends a rendered message to one accountant's WhatsApp number."""

    def send(self, to: str, message: str, link: Optional[str] = None) -> dict:
        result = send(to, message, link)
        print(f"  [Notify] Message to {to}: {result['status']}")
        return result

    def digest(self, to: str, cycle, base_url: Optional[str] = None) -> dict:
        """Send the month-end summary with a link to the full digest."""
        link = issue_link(cycle.cycle, firm_id=to, base_url=base_url)
        cycle.trail.add("source", f"Digest link sent to {to}")
        return self.send(to, cycle.whatsapp_summary(), link["url"])
