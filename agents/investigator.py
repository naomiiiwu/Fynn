"""Exception investigator.

Where the LLM belongs, and where it does not:

  - Classifying a known fee label is a lookup. Handled by services/classification.
  - Proposing a treatment for a label never seen before is a judgement. Handled here.
  - Explaining why a residual exists is a judgement. Handled here.

Every output is a suggestion with a confidence score, routed to a human for
approval. Nothing here posts to a ledger. This mirrors what practising
accountants told us they require: automation for the repeatable part, human
review for anything ambiguous.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from models.transaction import Evidence, ReconException, Side

# Sonnet, not Opus: this is a short, well-bounded judgement on a handful of
# lines per cycle, and the cost has to stay near zero at firm-wide volume.
MODEL = "claude-sonnet-5"

SYSTEM = """You assist a bookkeeper reconciling Southeast Asian marketplace settlements.

You never decide. You propose, with a confidence score, and a human approves.

Rules:
- If you cannot determine the treatment, say so and give low confidence. A wrong
  suggestion at high confidence is worse than no suggestion.
- Prefer standard accounts: Sales Revenue, Commission Expense, Payment Processing
  Fees, Marketing Expense, Sales Returns & Allowances, Shipping Expense,
  Other Income, or the platform Clearing Account.
- Amounts held by the platform but not paid out belong in the clearing account,
  not revenue.

Respond with JSON only, no prose, no markdown fences:
{"summary": str, "confidence": int 0-100, "suggested_account": str, "suggested_side": "debit"|"credit"}"""


def _client():
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return Anthropic(api_key=key)


def investigate(exc: ReconException, context: Optional[str] = None) -> Optional[Evidence]:
    """Propose an explanation for an exception the rules engine could not resolve.

    Returns None when no API key is configured — the exception then goes to the
    accountant with no suggestion attached, which is a valid state.
    """
    if exc.evidence is not None:
        return exc.evidence  # deterministic evidence already found; do not override

    client = _client()
    if client is None:
        return None

    line = exc.line
    prompt = f"""Platform: {exc.platform.value}
Line label: {line.label if line else 'n/a'}
Amount: {exc.amount}
Order reference: {line.order_id if line and line.order_id else 'none'}
Problem: {exc.why}
{f'Additional context: {context}' if context else ''}

What is the most likely treatment?"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        return Evidence(
            summary=data["summary"],
            confidence=int(data["confidence"]),
            suggested_account=data.get("suggested_account"),
            suggested_side=Side(data["suggested_side"]) if data.get("suggested_side") else None,
            source="llm",
        )
    except Exception:
        # An investigator failure must never block reconciliation. The exception
        # simply reaches the accountant without a suggestion.
        return None
