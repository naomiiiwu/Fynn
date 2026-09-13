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
from typing import Optional

from models.transaction import ReconException, SettlementLine

# Sonnet, not Opus: a short grounded answer about a handful of lines, run once
# per question an accountant asks. Cost has to stay near zero at firm volume.
MODEL = "claude-sonnet-5"
MAX_TURNS = 12

SYSTEM = """You are Fynn, working through one reconciliation exception with a qualified accountant.

You are talking to a professional. Be direct and specific. Do not pad, do not
flatter, do not explain what a refund is.

Your job is to get this line to a decision. That means:

- Propose an account by name, from the list below, and say what it rests on —
  the firm's own past decisions first, then the platform's own wording, then
  the shape of the amount. "I cannot tell" is an answer of last resort, and
  when it is the right answer, say what would settle it.
- Lean on what this firm has already decided. A label they have treated a
  certain way is the strongest evidence available, and stronger than anything
  you know about the platform in general.
- If one short question would genuinely change your answer, ask it instead of
  hedging. One question, not three.
- You explain and propose. You never decide, and you cannot approve — the
  accountant does that with the control beside this conversation.
- State confidence honestly. A wrong answer given confidently is worse for this
  user than an admission of uncertainty, because they may post it to a client's
  books. Say plainly when you are inferring from a fee's name rather than from
  the data.
- Two or three sentences unless asked for more. Amounts written plainly: 12.00.

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
             prior_lines: list[SettlementLine],
             rules: Optional[list] = None,
             accounts: Optional[list[str]] = None) -> str:
    same = [l for l in cycle_lines if l.platform == exc.platform]

    # Summarised by label, not one row per line. A real statement runs to
    # hundreds of lines; pasting them all buries the two facts that matter —
    # what else is in this settlement and how big this label is against it —
    # in noise, and costs a fortune per question.
    totals: dict[str, tuple[int, float]] = {}
    for l in same:
        count, total = totals.get(l.label, (0, 0.0))
        totals[l.label] = (count + 1, total + l.amount)
    rows = "\n".join(
        f"  {label:34} {count:>4} lines {total:>12.2f}"
        for label, (count, total) in sorted(
            totals.items(), key=lambda kv: -abs(kv[1][1]))
    ) or "  (none)"

    # The same label in earlier cycles: the strongest single clue about a fee
    # that has been seen before.
    label = (exc.line.label if exc.line else "").strip().lower()
    history = [l for l in prior_lines
               if l.platform == exc.platform and l.label.strip().lower() == label]
    prior = "\n".join(
        f"  {l.cycle}  {l.label:24} {l.amount:>10.2f}  {l.order_id or '-'}"
        for l in history[:20]
    ) or "  (this label has not appeared in an earlier cycle)"

    # What the firm has already decided. Nearest matches first: a decision about
    # a similarly named fee is the best evidence there is.
    decided = "  (this firm has not recorded any decisions yet)"
    if rules:
        import difflib

        def nearness(rule) -> float:
            name = (getattr(rule, "label", "") or "").strip().lower()
            if not name or not label:
                return 0.0
            return difflib.SequenceMatcher(None, label, name).ratio()

        ranked = sorted(rules, key=nearness, reverse=True)[:14]
        decided = "\n".join(
            f"  {(getattr(r, 'platform', None).value if getattr(r, 'platform', None) else 'any'):<12} "
            f"{(getattr(r, 'label', '') or getattr(r, 'category', '') or ''):34} -> {r.account}"
            for r in ranked
        ) or decided

    available = ", ".join(accounts) if accounts else "(not supplied)"

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

WHAT THIS FIRM HAS ALREADY DECIDED — nearest labels first
{decided}

ACCOUNTS YOU MAY PROPOSE — use these names exactly
  {available}

THIS SETTLEMENT — {exc.platform.value}, by label
{rows}

THIS LABEL IN EARLIER CYCLES
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
    rules: Optional[list] = None,
    accounts: Optional[list[str]] = None,
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
            # Replies are asked to be short, and occasionally are not. A ceiling
            # that truncates one mid-sentence reads as a fault in the product,
            # which costs more than the tokens do.
            max_tokens=900,
            system=SYSTEM + "\n\n" + _context(
                exc, cycle_lines, prior_lines, rules, accounts),
            messages=turns,
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        return f"I could not reach the model just now ({type(e).__name__}). The evidence above still stands."
