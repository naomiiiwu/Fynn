"""Conversational exception explanation.

This is the only conversational surface in the product. The digest is
structured; accountants who compared both preferred a report for reviewing and
verifying entries, and wanted conversation reserved for the items that actually
need explaining.

The model is given one exception and the cycle it sits in. It answers questions
about that exception. It cannot approve anything — approval is a separate,
explicit action taken by the accountant.
"""
from __future__ import annotations

import os

from models.transaction import ReconException, SettlementLine

# Sonnet, not Opus: a short grounded answer about a handful of lines, run once
# per question an accountant asks. Cost has to stay near zero at firm volume.
MODEL = "claude-sonnet-5"
MAX_TURNS = 12

SYSTEM = """You are Fynn, explaining one reconciliation exception to a qualified accountant.

You are talking to a professional. Be direct and specific. Do not pad, do not
flatter, do not explain what a refund is.

Hard rules:
- You explain and propose. You never decide. The accountant approves separately.
- Ground every claim in the cycle data given below. If the data does not support
  an answer, say so plainly rather than speculating.
- State confidence honestly. A wrong answer given confidently is worse for this
  user than an admission of uncertainty, because they may post it to a client's books.
- Keep replies to two or three sentences unless asked for detail.
- Amounts: write them plainly, e.g. 12.00.

You are not a general assistant. If asked about anything outside this exception
and its cycle, say it is outside what you can see here."""


def _client():
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY")
    return Anthropic(api_key=key) if key else None


def _context(exc: ReconException, cycle_lines: list[SettlementLine],
             prior_lines: list[SettlementLine]) -> str:
    same = [l for l in cycle_lines if l.platform == exc.platform]
    rows = "\n".join(
        f"  {l.label:22} {l.amount:>10.2f}  {l.order_id or '-'}  {l.date or '-'}"
        for l in same
    )
    prior = "\n".join(
        f"  {l.cycle}  {l.label:18} {l.amount:>10.2f}  {l.order_id or '-'}"
        for l in prior_lines if l.platform == exc.platform
    ) or "  (none)"

    ev = ""
    if exc.evidence:
        ev = (
            f"\nWhat you already found: {exc.evidence.summary} "
            f"(confidence {exc.evidence.confidence}%, source: {exc.evidence.source})"
        )

    return f"""EXCEPTION
  Platform:  {exc.platform.value}
  Type:      {exc.kind}
  Amount:    {exc.amount:.2f}
  Label:     {exc.line.label if exc.line else 'n/a'}
  Order:     {exc.line.order_id if exc.line and exc.line.order_id else 'none'}
  Platform note: {exc.line.note if exc.line and exc.line.note else 'none'}
  Problem:   {exc.why}{ev}

THIS CYCLE — {exc.platform.value}
{rows}

EARLIER CYCLES — {exc.platform.value}
{prior}"""


def opening_message(exc: ReconException) -> str:
    """First thing Fynn says when the drawer opens. Deterministic, no LLM call."""
    amt = f"{abs(exc.amount):.2f}"
    if exc.kind == "orphan_refund":
        base = (
            f"There is a refund of {amt} against "
            f"{exc.line.order_id if exc.line else 'an order'} with no matching sale in this cycle."
        )
    elif exc.kind == "partial_refund":
        base = f"A refund of {amt} looks like a partial refund against an earlier order."
    elif exc.kind == "unknown_label":
        label = exc.line.label if exc.line else "this line"
        base = (
            f'A charge labelled "{label}" came through for {amt}. '
            "No rule exists for it on this account, so I have not assigned it an account."
        )
    elif exc.kind == "withheld_balance":
        base = (
            f"{amt} is settled but not paid. The platform is holding it in the seller "
            "balance rather than releasing it with this payout."
        )
    else:
        base = f"The cycle does not tie out. There is {amt} I could not place."

    if exc.evidence:
        base += f" {exc.evidence.summary} Confidence {exc.evidence.confidence}%."
    return base


def reply(
    exc: ReconException,
    history: list[dict],
    cycle_lines: list[SettlementLine],
    prior_lines: list[SettlementLine],
) -> str:
    """Continue the conversation about this exception."""
    client = _client()
    if client is None:
        return (
            "I cannot answer follow-up questions without an API key configured. "
            "The evidence above is what I found deterministically from the cycle data."
        )

    turns = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-MAX_TURNS:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not turns or turns[-1]["role"] != "user":
        return "Ask me anything about this exception."

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=SYSTEM + "\n\n" + _context(exc, cycle_lines, prior_lines),
            messages=turns,
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return f"I could not reach the model just now ({type(e).__name__}). The evidence above still stands."
