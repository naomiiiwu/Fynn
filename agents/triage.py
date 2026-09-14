"""Propose a treatment for every open exception at once.

The single-exception investigator answers "what is this fee?". This answers
"what should this close do?", which is the question an accountant actually
arrives with — and it is a different question, not a louder version of the same
one. Deciding nine lines together means the model can see that two of them are
the same argument, that one contradicts a rule the firm already wrote, and how
large each is against the settlement it sits in. Deciding them one at a time
cannot see any of that, and costs nine round trips to find out.

Why this is safe to do in bulk, when classifying every line would not be:

  - Nothing here reaches a ledger. Each proposal is an account name against an
    exception key, and an accountant accepts, edits or ignores it.
  - Every proposal becomes an ordinary approval, so it inherits the checks that
    already exist: the entry must balance, the cycle must tie to the payout, and
    a rule is written that decides the same label without asking next time.
  - A proposal Fynn is unsure of is returned unsure. Confidence is the model's
    own, and the screen does not pre-select anything below a threshold, because
    an unreviewed wrong answer is the one failure this product cannot have.

The asset is still the rules. This is a faster way to write them, not a
replacement for them: a label decided here is decided for good, so the work
shrinks every cycle rather than recurring.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from models.transaction import ReconException, SettlementLine

MODEL = "claude-sonnet-5"

# Below this a proposal is shown but never pre-selected. An accountant skimming
# a list of confident-looking suggestions is exactly how a wrong one gets
# posted, so the ones Fynn is unsure about have to look unsure.
TRUSTED = 70

SYSTEM = """You are Fynn, proposing how an accountant should treat every unresolved line in one marketplace settlement.

You are addressing a qualified accountant who will review each proposal before
anything is posted. Be specific and brief. Do not explain what a refund is.

For each exception, choose the account it should post to, from the list given.

What to weigh, in order:
- What this firm has already decided. A label they have treated a certain way
  is stronger evidence than anything you know about the platform in general,
  and two labels that mean the same thing should be treated the same way.
- What the platform's own wording says the fee is for.
- The sign and size of the amount. A credit is money coming in; a large one
  against a small settlement deserves more caution, not less.

Rules:
- Tax withheld, or tax the platform collected, never goes to an expense
  account. It is a balance-sheet item.
- Money the platform is holding and has not released belongs in the clearing
  account, not in revenue.
- A discount or voucher that reduces what the seller receives is a reduction of
  revenue, not a cost of sale, unless the firm has decided otherwise.
- Confidence is yours to state honestly. Below 70 means "I would not act on
  this without checking", and saying so is always better than being wrong
  confidently — these post to a client's books.

Respond with JSON only. No prose, no markdown fences:
{"proposals": [{"key": str, "account": str, "confidence": int, "reason": str}]}

`key` must be copied exactly from the exception. `account` must be exactly one
of the accounts listed. `reason` is one sentence, no more."""


def _parse(text: str) -> Optional[dict]:
    """Read the reply, salvaging a truncated array rather than losing it whole.

    A reply cut off mid-object is still eight good proposals and one broken
    one. Discarding all nine because the last is incomplete throws away the
    work and the money spent on it.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    end = text.rfind("}")
    while end != -1:
        try:
            return json.loads(text[:end + 1] + "]}")
        except json.JSONDecodeError:
            end = text.rfind("}", 0, end)
    return None


def _confidence(value) -> int:
    """Read a confidence on whichever scale it arrived in.

    Asked for 0-100, a model sometimes answers 0.85. int(0.85) is 0 — no error,
    no warning, every proposal silently untrusted and nothing pre-selected. It
    reads as the model having no opinion when it was confident, which is the
    most misleading way to be wrong about confidence.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 < number <= 1:
        number *= 100
    return max(0, min(100, round(number)))


def _client():
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY")
    return Anthropic(api_key=key) if key else None


def _context(
    exceptions: list[ReconException],
    lines: list[SettlementLine],
    rules: list,
    accounts: list[str],
) -> str:
    """Everything the model needs, and nothing it does not.

    The settlement is summarised by label rather than listed line by line: a
    real export runs to hundreds of rows, and what matters is how large each
    label is against the rest, which a summary states and a listing buries.
    """
    totals: dict[str, tuple[int, float]] = {}
    for line in lines:
        count, total = totals.get(line.label, (0, 0.0))
        totals[line.label] = (count + 1, total + line.amount)
    settlement = "\n".join(
        f"  {label:38} {count:>4} lines {total:>12.2f}"
        for label, (count, total) in sorted(totals.items(), key=lambda kv: -abs(kv[1][1]))
    )

    decided = "\n".join(
        f"  {(r.platform.value if getattr(r, 'platform', None) else 'any'):<12} "
        f"{(getattr(r, 'label', '') or getattr(r, 'category', '') or ''):38} -> {r.account}"
        for r in (rules or [])[:40]
    ) or "  (this firm has not recorded any decisions yet)"

    items = []
    for exc in exceptions:
        line = exc.line
        items.append(
            f"""  key:      {exc.key}
  platform: {exc.platform.value}
  kind:     {exc.kind}
  label:    {line.label if line else 'n/a'}
  amount:   {exc.amount:.2f}
  note:     {line.note if line and line.note else 'none'}
  problem:  {exc.why}"""
        )

    return f"""ACCOUNTS YOU MAY PROPOSE — use these names exactly
  {", ".join(accounts)}

WHAT THIS FIRM HAS ALREADY DECIDED
{decided}

THE SETTLEMENT, BY LABEL
{settlement}

UNRESOLVED — propose one account for each
{chr(10).join(items)}"""


def triage(
    exceptions: list[ReconException],
    lines: list[SettlementLine],
    rules: Optional[list] = None,
    accounts: Optional[list[str]] = None,
) -> dict:
    """One proposal per exception. Never decides; never posts.

    Returns {"proposals": [...], "error": str}. An empty list is a valid
    answer — no key configured, the model unreachable, a reply that did not
    parse — and the accountant carries on exception by exception as before.
    """
    if not exceptions:
        return {"proposals": [], "error": ""}

    client = _client()
    if client is None:
        return {"proposals": [], "error":
                "No ANTHROPIC_API_KEY is configured, so Fynn cannot propose treatments."}

    allowed = list(accounts or [])
    try:
        response = client.messages.create(
            model=MODEL,
            # One object per exception, each carrying a sentence. A cycle with
            # thirty unresolved labels is not unusual, and a ceiling that
            # truncates the array mid-object loses every proposal in it, not
            # just the last.
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": _context(
                exceptions, lines, rules or [], allowed)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        payload = _parse(text)
        if payload is None:
            return {"proposals": [], "error": (
                "Fynn's reply could not be read as proposals. Decide these by "
                "hand, or try again." if response.stop_reason != "max_tokens" else
                "There were too many exceptions to propose in one pass. Decide "
                "some by hand and try again.")}
    except Exception as exc:
        return {"proposals": [], "error":
                f"Could not reach the model ({type(exc).__name__})."}

    # Only proposals that name a real exception and a real account survive. A
    # model that invents either is proposing something nobody can act on, and
    # dropping it silently is better than showing an account that does not exist.
    keys = {e.key for e in exceptions}
    names = {a.lower(): a for a in allowed}
    clean = []
    for row in payload.get("proposals", []):
        key = str(row.get("key", ""))
        account = names.get(str(row.get("account", "")).strip().lower())
        if key not in keys or account is None:
            continue
        confidence = _confidence(row.get("confidence"))
        clean.append({
            "key": key,
            "account": account,
            "confidence": confidence,
            "reason": str(row.get("reason", "")).strip()[:240],
            "trusted": confidence >= TRUSTED,
        })
    return {"proposals": clean, "error": ""}
