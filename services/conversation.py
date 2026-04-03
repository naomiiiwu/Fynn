"""
Fynn conversational agent for WhatsApp.

Handles incoming WhatsApp messages via Twilio webhook, maintains
per-sender conversation history, and routes to the right action:
  - "run report" / "send report" → triggers full pipeline
  - anything else → Claude answers as Fynn using P&L context
"""

import json
import os
from typing import Optional

import anthropic

SYSTEM_PROMPT = """You are Fynn, an autonomous AI bookkeeping agent for cross-border e-commerce sellers.

You communicate via WhatsApp, so keep replies short and clear — no long walls of text.
Use plain English. Use numbers when relevant. Be friendly but professional.

You help sellers with:
- Understanding their monthly P&L (revenue, costs, profit margin)
- Explaining anomalies or unusual transactions
- Answering questions about their Shopee sales, refunds, and fees
- Triggering their monthly bookkeeping report

If the seller asks to run or generate their report, tell them you're on it and that they'll receive a WhatsApp update shortly.

If you don't have P&L data yet, tell them to trigger their report first by saying "run my report".

Keep replies under 5 sentences. No markdown headers. Use emojis sparingly."""

# Trigger phrases that kick off the full pipeline
REPORT_TRIGGERS = {
    "run report", "run my report", "generate report", "send report",
    "monthly report", "send my report", "get report", "start report",
    "run", "go", "start",
}


class ConversationManager:
    """
    Manages per-sender conversation history and Claude responses.

    Each sender (WhatsApp number) gets their own message history
    so Claude maintains context across a conversation session.
    """

    def __init__(self) -> None:
        """Initialise with empty conversation store and Anthropic client."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        # phone_number -> list of {role, content} message dicts
        self._history: dict[str, list[dict]] = {}
        # phone_number -> last P&L dict
        self._pnl_store: dict[str, dict] = {}

    def store_pnl(self, phone: str, pnl: dict) -> None:
        """
        Store the latest P&L for a sender so Claude can reference it.

        Args:
            phone: Sender's WhatsApp number (e.g. 'whatsapp:+6591234567').
            pnl:   The P&L dict from the last pipeline run.
        """
        self._pnl_store[phone] = pnl

    def get_pnl(self, phone: str) -> Optional[dict]:
        """
        Retrieve the stored P&L for a sender.

        Args:
            phone: Sender's WhatsApp number.

        Returns:
            P&L dict or None if not yet generated.
        """
        return self._pnl_store.get(phone)

    def is_report_trigger(self, message: str) -> bool:
        """
        Check if the incoming message is asking to run the report.

        Args:
            message: Raw message text from the seller.

        Returns:
            True if the message is a report trigger phrase.
        """
        cleaned = message.strip().lower().rstrip("!?.").strip()
        return cleaned in REPORT_TRIGGERS

    def reply(self, phone: str, incoming: str) -> str:
        """
        Generate a Fynn reply to an incoming WhatsApp message.

        Maintains conversation history per sender. Injects P&L context
        into the system prompt if available.

        Args:
            phone:    Sender's WhatsApp number.
            incoming: The message text received.

        Returns:
            Fynn's reply as a plain string (sent back via Twilio).
        """
        if not self.client:
            return (
                "Hey! I'm Fynn, your AI bookkeeper. "
                "I'm not fully set up yet — ask your account manager to check the API config."
            )

        # Initialise history for new senders
        if phone not in self._history:
            self._history[phone] = []

        # Append sender's message
        self._history[phone].append({"role": "user", "content": incoming})

        # Build system prompt — inject P&L if available
        pnl = self.get_pnl(phone)
        system = SYSTEM_PROMPT
        if pnl:
            system += f"\n\nHere is the seller's most recent P&L report for context:\n{json.dumps(pnl, indent=2)}"
        else:
            system += "\n\nNo P&L report has been generated yet for this seller."

        # Keep history to last 20 messages to avoid token bloat
        recent_history = self._history[phone][-20:]

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=system,
                messages=recent_history,
            )
            reply_text = response.content[0].text.strip()
        except Exception as exc:
            print(f"  [Conversation] Claude error: {exc}")
            reply_text = "Sorry, I hit a snag — please try again in a moment. 🙏"

        # Append Fynn's reply to history
        self._history[phone].append({"role": "assistant", "content": reply_text})

        print(f"  [Conversation] {phone} → {incoming[:60]}")
        print(f"  [Conversation] Fynn → {reply_text[:60]}")

        return reply_text

    def clear_history(self, phone: str) -> None:
        """
        Clear conversation history for a sender (e.g. on 'reset' command).

        Args:
            phone: Sender's WhatsApp number.
        """
        self._history.pop(phone, None)
