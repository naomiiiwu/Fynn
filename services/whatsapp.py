"""WhatsApp delivery of the digest link.

The link is tokenised and expiring — no login. Accountants receive it the way
they already receive everything else from clients, and open it in one tap.

Twilio is the transport in both directions: this module sends the link out, and
main.py's /webhook/whatsapp takes messages and settlement files in.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

LINK_TTL_DAYS = 14

# token -> {cycle, firm_id, expires}. Production: a table, not a dict — a
# restart invalidates every link that has been sent out.
_LINKS: dict[str, dict] = {}


def issue_link(
    cycle: str, firm_id: Optional[str] = None, base_url: Optional[str] = None
) -> dict:
    """Mint a link to one firm's open cycle.

    The token carries the firm, so the page it opens is scoped to whoever the
    link was sent to — a token from one firm cannot read another's cycle.
    """
    token = secrets.token_urlsafe(9)
    expires = datetime.now(timezone.utc) + timedelta(days=LINK_TTL_DAYS)
    _LINKS[token] = {"cycle": cycle, "firm_id": firm_id, "expires": expires}
    base = (base_url or os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")).rstrip("/")
    return {"token": token, "url": f"{base}/c/{token}", "expires": expires.isoformat()}


def resolve(token: str) -> Optional[dict]:
    entry = _LINKS.get(token)
    if entry is None:
        return None
    if entry["expires"] < datetime.now(timezone.utc):
        _LINKS.pop(token, None)
        return None
    return entry


def _wa(number: str) -> str:
    """Normalise to Twilio's WhatsApp channel form, idempotently.

    TWILIO_WHATSAPP_FROM is conventionally stored already prefixed, so blindly
    prepending would produce 'whatsapp:whatsapp:+1...' and a 400 from Twilio.
    """
    value = (number or "").strip()
    if not value or value.startswith("whatsapp:"):
        return value
    return f"whatsapp:{value}"


def send(to: str, message: str, link: Optional[str] = None) -> dict:
    """Send via Twilio WhatsApp. Falls back to console when unconfigured."""
    body = message
    if link:
        body += (
            f"\n\nOpen digest: {link}"
            f"\n\nNo login needed. Link expires in {LINK_TTL_DAYS} days."
        )

    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

    if not (sid and token and sender):
        print("\n--- WhatsApp (not configured, printing instead) ---")
        print(f"To: {to}\n{body}")
        print("---\n")
        return {"status": "console", "to": to, "body": body}

    try:
        from twilio.rest import Client
        client = Client(sid, token)
        msg = client.messages.create(from_=_wa(sender), to=_wa(to), body=body)
        return {"status": "sent", "sid": msg.sid, "to": to}
    except Exception as e:
        print(f"  [WhatsApp] Send failed: {type(e).__name__}: {e}")
        return {"status": "failed", "error": f"{type(e).__name__}: {e}", "to": to}
