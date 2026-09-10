"""WhatsApp message routing.

The digest page is where reviewing and approving happen. WhatsApp is how the
link gets to the accountant and how settlement files come back — so this is a
small router, not a chat agent. Anything it does not recognise is answered with
the digest link, which is almost always what the sender wanted.

Approvals are deliberately not a WhatsApp command. An approval is an accounting
decision recorded against a named person; it belongs on a surface where the
evidence, the amounts and the resulting journal are all visible at once.
"""
from __future__ import annotations

from dataclasses import dataclass

QUICK_START_EN = (
    "*How Fynn works*\n"
    "1. Send a settlement CSV (Shopee, Lazada or TikTok Shop).\n"
    "2. I classify every line and check it against the reported payout.\n"
    "3. You get a link to the month-end digest — no login.\n"
    "4. Anything that does not tie out is waiting there for your approval.\n"
    "5. Nothing posts until you approve it."
)

QUICK_START_ZH = (
    "*Fynn 的运作方式*\n"
    "1. 传送结算 CSV（Shopee、Lazada 或 TikTok Shop）。\n"
    "2. 我会分类每一行，并与平台申报的付款金额核对。\n"
    "3. 你会收到月结摘要的链接，无需登录。\n"
    "4. 无法对上的项目会在那里等你批准。\n"
    "5. 未经你批准，不会入账。"
)

COMMANDS_HELP = (
    "*What you can send me*\n"
    "• a settlement CSV — I'll reconcile it and send the digest link\n"
    "• *digest* — a fresh link to the current cycle\n"
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
    "digest", "status", "summary", "report", "cycle", "review", "recap", "link",
}


@dataclass
class Command:
    """One parsed instruction from an incoming WhatsApp message."""

    kind: str          # greeting | setup | digest | unknown


def _normalise(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def parse_command(message: str) -> Command:
    """Map an incoming message onto a command. Never raises."""
    text = _normalise(message)
    if not text:
        return Command("unknown")
    if text in _SETTINGS_TRIGGERS:
        return Command("setup")
    if text in _GREETINGS:
        return Command("greeting")
    if text in _DIGEST_TRIGGERS:
        return Command("digest")
    return Command("unknown")
