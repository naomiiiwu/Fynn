"""WhatsApp command grammar.

The accountants who reviewed both surfaces wanted the structured digest for
reviewing figures and conversation only for individual exceptions — so this is
a small, predictable command parser, not a chat agent. An approval is an
accounting decision that gets a named actor and a timestamp against it; it must
mean exactly what it says, which rules out inferring intent with a model.

Free text that matches nothing is answered with the command list rather than
guessed at.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from models.transaction import Side

QUICK_START_EN = (
    "*How Fynn works*\n"
    "1. Send a settlement CSV (Shopee, Lazada or TikTok Shop).\n"
    "2. I classify every line and check it against the reported payout.\n"
    "3. Anything that does not tie out comes back to you as a numbered exception.\n"
    "4. You approve — nothing posts unreviewed — and I save the decision as a rule.\n"
    "5. Reply *post* and I build one balanced journal per platform."
)

QUICK_START_ZH = (
    "*Fynn 的运作方式*\n"
    "1. 传送结算 CSV（Shopee、Lazada 或 TikTok Shop）。\n"
    "2. 我会分类每一行，并与平台申报的付款金额核对。\n"
    "3. 无法对上的项目会以编号例外的形式回覆你。\n"
    "4. 你批准后我才入账，并把这个决定存成规则。\n"
    "5. 回覆 *post*，我会为每个平台生成一张平衡的分录。"
)

COMMANDS_HELP = (
    "*What you can send me*\n"
    "• a settlement CSV — I'll reconcile it\n"
    "• *digest* — the current cycle, with open exceptions\n"
    "• *why 1* — the evidence behind exception 1\n"
    "• *approve 1* — accept the suggested treatment\n"
    "• *approve 1 Marketing Expense* — choose the account\n"
    "• *approve 1 Other Income credit* — choose account and side\n"
    "• *post* — send the journals to your ledger\n"
    "• *rules* — the rules your firm has accumulated\n"
    "• *setup* — change firm settings"
)

_GREETINGS = {
    "hi", "hey", "hello", "help", "start", "yo", "menu", "commands",
    "你好", "哈啰", "开始",
}
_SETTINGS_TRIGGERS = {
    "setup", "settings", "reset", "restart", "clear", "preferences", "设置",
}
_DIGEST_TRIGGERS = {
    "digest", "status", "summary", "report", "cycle", "review", "recap",
}
_POST_TRIGGERS = {"post", "post entries", "send to ledger", "publish"}
_RULES_TRIGGERS = {"rules", "my rules", "rule list"}


@dataclass
class Command:
    """One parsed instruction from an incoming WhatsApp message."""

    kind: str                          # greeting | setup | digest | why | approve
                                       # | post | rules | unknown
    index: Optional[int] = None        # exception number, for why/approve
    account: Optional[str] = None      # account named in an approval
    side: Optional[Side] = None        # side named in an approval


def _normalise(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def parse_command(message: str) -> Command:
    """Map an incoming message onto a command. Never raises."""
    text = _normalise(message)
    if not text:
        return Command("unknown")

    if text in _GREETINGS:
        return Command("greeting")
    if text in _SETTINGS_TRIGGERS:
        return Command("setup")
    if text in _DIGEST_TRIGGERS:
        return Command("digest")
    if text in _POST_TRIGGERS:
        return Command("post")
    if text in _RULES_TRIGGERS:
        return Command("rules")

    why = re.match(r"^(?:why|explain|evidence|detail)\s+#?(\d+)$", text)
    if why:
        return Command("why", index=int(why.group(1)))

    approve = re.match(r"^(?:approve|accept|ok|yes)\s+#?(\d+)\s*(.*)$", text)
    if approve:
        index = int(approve.group(1))
        remainder = (approve.group(2) or "").strip()
        side: Optional[Side] = None

        trailing = re.search(r"\b(debit|credit)$", remainder)
        if trailing:
            side = Side(trailing.group(1))
            remainder = remainder[: trailing.start()].strip()

        # The account name is echoed back to the accountant and written to the
        # audit trail, so restore the casing the message lost in normalising.
        account = _restore_case(message, remainder) if remainder else None
        return Command("approve", index=index, account=account, side=side)

    # A bare number is the most common shorthand once the digest is on screen.
    bare = re.match(r"^#?(\d+)$", text)
    if bare:
        return Command("approve", index=int(bare.group(1)))

    return Command("unknown")


def _restore_case(original: str, normalised_fragment: str) -> str:
    """Recover the sender's original casing for an account name."""
    if not normalised_fragment:
        return normalised_fragment
    pattern = r"\s+".join(re.escape(word) for word in normalised_fragment.split())
    match = re.search(pattern, original, flags=re.IGNORECASE)
    return match.group(0).strip() if match else normalised_fragment.title()
