"""
Fynn conversational agent for WhatsApp.

Handles incoming WhatsApp messages via Twilio webhook:
  - New users → onboarding flow (state machine)
  - Existing users → conversational Q&A or report trigger
  - "change settings" → restart onboarding
"""

import json
import os
from typing import Optional

import anthropic

from models.user_profile import ProfileStore, UserProfile

# ── Strings (English) ──────────────────────────────────────────────────────────

EN = {
    "welcome": (
        "Hey! 👋 I'm *Fynn*, your AI bookkeeper.\n"
        "I'll handle your e-commerce books automatically — "
        "reconciliation, P&L, multi-currency, all on WhatsApp.\n\n"
        "Let's get set up in 60 seconds. What's your name?"
    ),
    "ask_frequency": (
        "Nice to meet you, {name}! 🎉\n\n"
        "How often do you want reports?\n"
        "Reply with any combination:\n\n"
        "1️⃣ Daily order ping\n"
        "2️⃣ Weekly summary\n"
        "3️⃣ Monthly full P&L\n\n"
        "_(e.g. reply *2 3* for weekly + monthly)_"
    ),
    "ask_time": (
        "Got it! ✅\n\n"
        "What time should I send your reports?\n"
        "_(Reply with a number, e.g. *9* for 9AM)_"
    ),
    "ask_currency": (
        "Perfect. Which currency for your reports?\n\n"
        "1️⃣ SGD — Singapore Dollar\n"
        "2️⃣ MYR — Malaysian Ringgit\n"
        "3️⃣ USD — US Dollar"
    ),
    "ask_language": (
        "Last one — preferred language?\n\n"
        "1️⃣ English\n"
        "2️⃣ 中文 (Mandarin)"
    ),
    "complete": (
        "You're all set, {name}! 🚀\n\n"
        "Here's your Fynn config:\n"
        "─────────────────────\n"
        "{summary}\n"
        "─────────────────────\n\n"
        "You can ask me anything about your books, or say:\n"
        "• *run my report* — generate your P&L now\n"
        "• *change settings* — update your preferences\n"
        "• *reset* — restart setup from scratch\n\n"
        "— Fynn 🤖"
    ),
    "settings_updated": (
        "Settings updated! ✅\n\n"
        "{summary}\n\n"
        "— Fynn 🤖"
    ),
    "invalid_frequency": "Please reply with numbers like *1*, *2*, *3*, or a combo like *1 2*.",
    "invalid_time": "Please reply with just a number between 0 and 23, e.g. *9* for 9AM.",
    "invalid_currency": "Please reply with *1*, *2*, or *3*.",
    "invalid_language": "Please reply with *1* for English or *2* for 中文.",
}

ZH = {
    "welcome": (
        "你好！👋 我是 *Fynn*，你的AI记账助手。\n"
        "我会自动处理你的电商账目——对账、盈亏报告、多币种转换，全部在WhatsApp上完成。\n\n"
        "让我们用60秒完成设置。请问你叫什么名字？"
    ),
    "ask_frequency": (
        "很高兴认识你，{name}！🎉\n\n"
        "你希望多久收到报告？\n"
        "请回复任意组合：\n\n"
        "1️⃣ 每日订单提醒\n"
        "2️⃣ 每周汇总\n"
        "3️⃣ 每月完整盈亏报告\n\n"
        "_(例如回复 *2 3* 表示每周+每月)_"
    ),
    "ask_time": "好的！✅\n\n几点发送报告给你？\n_(回复数字，例如 *9* 表示早上9点)_",
    "ask_currency": "完美。报告使用哪种货币？\n\n1️⃣ SGD — 新加坡元\n2️⃣ MYR — 马来西亚令吉\n3️⃣ USD — 美元",
    "ask_language": "最后一步——首选语言？\n\n1️⃣ English\n2️⃣ 中文",
    "complete": (
        "设置完成，{name}！🚀\n\n"
        "你的Fynn配置：\n"
        "─────────────────────\n"
        "{summary}\n"
        "─────────────────────\n\n"
        "你可以随时问我账目问题，或者说：\n"
        "• *生成报告* — 立即生成盈亏报告\n"
        "• *修改设置* — 更新偏好设置\n"
        "• *reset* — 重新开始设置\n\n"
        "— Fynn 🤖"
    ),
    "settings_updated": "设置已更新！✅\n\n{summary}\n\n— Fynn 🤖",
    "invalid_frequency": "请回复数字，如 *1*、*2*、*3* 或组合 *1 2*。",
    "invalid_time": "请回复0到23之间的数字，例如 *9* 表示早上9点。",
    "invalid_currency": "请回复 *1*、*2* 或 *3*。",
    "invalid_language": "请回复 *1* 表示English，*2* 表示中文。",
}

REPORT_TRIGGERS_EN = {
    "run report", "run my report", "generate report", "send report",
    "monthly report", "send my report", "get report", "start report",
    "run", "go", "start",
}

REPORT_TRIGGERS_ZH = {"生成报告", "运行报告", "发送报告", "开始报告"}

GREETING_TRIGGERS_EN = {
    "hi", "hello", "hey", "yo", "start", "help", "menu",
}
GREETING_TRIGGERS_ZH = {"你好", "嗨", "开始", "帮助", "菜单"}

# Valid manual file type labels (when user clarifies an unknown file)
FILE_TYPE_LABELS = {
    "shopee": ("shopee", "transactions"),
    "lazada": ("lazada", "transactions"),
    "amazon": ("amazon", "transactions"),
    "shopify": ("shopify", "transactions"),
    "tiktok": ("tiktok", "transactions"),
    "cogs": ("generic", "cogs"),
    "cost of goods": ("generic", "cogs"),
    "supplier": ("generic", "cogs"),
    "ads": ("generic", "ads"),
    "advertising": ("generic", "ads"),
    "marketing": ("generic", "ads"),
    "warehouse": ("generic", "warehouse"),
    "storage": ("generic", "warehouse"),
    "payroll": ("generic", "payroll"),
    "staff": ("generic", "payroll"),
    "labour": ("generic", "payroll"),
    "labor": ("generic", "payroll"),
    "packaging": ("generic", "packaging"),
    "expense": ("generic", "expense"),
    "expenses": ("generic", "expense"),
    "other": ("generic", "expense"),
}

SETTINGS_TRIGGERS_EN = {
    "change settings", "settings", "update settings", "preferences", "setup",
    "change currency", "update currency", "change language", "update language",
    "change name", "update name", "change time", "update time",
    "change frequency", "update frequency", "edit settings", "my settings",
}
SETTINGS_TRIGGERS_ZH = {"修改设置", "设置", "更新设置", "修改货币", "修改语言", "修改名字"}

GUIDE_EN = """Here's how to use Fynn 📖

*Step 1 — Send your files*
Just drop your CSV files directly here in WhatsApp. I'll figure out what each one is.

Supported files:
• 🛒 Shopee Finance export _(Finance → My Income → Export)_
• 🛒 Lazada Finance report _(Finance → Transaction → Export)_
• 📦 Supplier / COGS sheet
• 📣 Ads spend export
• 🏭 Warehouse / storage costs
• 👥 Payroll / staff costs
• 📫 Packaging costs
• 💸 Any other business expense

*Step 2 — Run your report*
Once files are sent, say:
  *run my report*

I'll reconcile your payouts, detect anomalies, and send your full P&L here + save it to Google Sheets.

*Other commands*
• Ask me anything: _"What was my profit margin?"_
• *change settings* — update name, currency, language
• *reset* — restart setup

Monthly reports run automatically on your chosen schedule.
I've got it from here 🤖 — Fynn"""

QUICK_START_EN = """Quick start with Fynn 📖

What I do:
• Organise your e-commerce bookkeeping
• Reconcile payouts and expenses
• Generate your P&L summary

What you should send me:
• Shopee, Lazada or TikTok Shop finance CSV exports
• COGS, ads, warehouse, payroll, packaging, or other expense files

What you can type:
• *run my report* — build your report
• *Ask a question* — e.g. _"What was my profit?"_
• *change settings* — update preferences"""

GUIDE_ZH = """Fynn使用指南 📖

*第一步 — 发送文件*
直接在WhatsApp发送你的CSV文件，我会自动识别每个文件的类型。

支持的文件：
• 🛒 Shopee财务导出 _(财务 → 我的收入 → 导出)_
• 🛒 Lazada财务报告 _(财务 → 交易 → 导出)_
• 🛒 TikTok Shop财务报告 _(财务 → 账单 → 导出)_
• 📦 供应商/货品成本表
• 📣 广告费用导出
• 🏭 仓储/履行费用
• 👥 工资/人力成本
• 📫 包装材料费用
• 💸 其他业务费用

*第二步 — 生成报告*
文件发送后，说：
  *生成报告*

我会自动对账、检测异常，并在这里发送盈亏报告，同时保存到Google Sheets。

*其他命令*
• 随时提问：_"我的利润率是多少？"_
• *修改设置* — 更新姓名、货币、语言
• *reset* — 重新开始设置

报告会按你设定的时间自动发送，剩下的交给我 🤖 — Fynn"""

QUICK_START_ZH = """Fynn快速开始 📖

我可以帮你：
• 整理电商账目
• 对账收入和费用
• 生成盈亏报告

你应该发送给我：
• Shopee、Lazada 或 TikTok Shop 财务CSV导出
• 货品成本、广告、仓储、工资、包装或其他费用文件

你可以这样输入：
• *生成报告* — 立即生成报告
• *直接提问* — 例如 _”我的利润是多少？”_
• *修改设置* — 更新偏好设置"""

CLAUDE_SYSTEM_EN = """You are Fynn, an autonomous AI bookkeeper for cross-border e-commerce sellers.
Communicate via WhatsApp — keep replies short and clear, no long paragraphs.
Use plain English, include numbers when relevant. Be friendly but professional.
If you don't have P&L data, tell the seller to say "run my report" first.
Keep replies under 5 sentences."""

CLAUDE_SYSTEM_ZH = """你是Fynn，一个跨境电商卖家的AI记账助手。
通过WhatsApp沟通——保持简短清晰，不要长篇大论。
用简单中文，数字要准确。友好但专业。
如果没有盈亏数据，告诉卖家先说"生成报告"。
回复不超过5句话。"""


class ConversationManager:
    """
    Manages per-sender conversation history, onboarding, and Claude replies.

    State machine steps:
      ask_name → ask_frequency → ask_time → ask_currency →
      ask_language → complete (None)
    """

    def __init__(self) -> None:
        """Initialise with empty stores and Anthropic client."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        self.profiles = ProfileStore()
        self._history: dict[str, list[dict]] = {}
        self._pnl_store: dict[str, dict] = {}
        self._pending_guide: dict[str, str] = {}  # phone → guide message to send after completion

    def pop_pending_guide(self, phone: str) -> Optional[str]:
        """Return and clear the pending guide message for a sender, if any."""
        return self._pending_guide.pop(phone, None)

    # ── Public API ─────────────────────────────────────────────────────────────

    def store_pnl(self, phone: str, pnl: dict) -> None:
        """Store the latest P&L for a sender."""
        self._pnl_store[phone] = pnl

    def get_pnl(self, phone: str) -> Optional[dict]:
        """Retrieve the stored P&L for a sender."""
        return self._pnl_store.get(phone)

    def get_profile(self, phone: str) -> Optional[UserProfile]:
        """Retrieve the profile for a sender."""
        return self.profiles.get(phone)

    def is_report_trigger(self, message: str, profile: Optional[UserProfile] = None) -> bool:
        """Check if message is asking to run the pipeline."""
        cleaned = message.strip().lower().rstrip("!?.").strip()
        lang = profile.language if profile else "en"
        if lang == "zh":
            return cleaned in REPORT_TRIGGERS_ZH or cleaned in REPORT_TRIGGERS_EN
        return cleaned in REPORT_TRIGGERS_EN

    def is_settings_trigger(self, message: str, profile: Optional[UserProfile] = None) -> bool:
        """Check if message is asking to change settings."""
        cleaned = message.strip().lower().rstrip("!?.").strip()
        lang = profile.language if profile else "en"
        if lang == "zh":
            return cleaned in SETTINGS_TRIGGERS_ZH or cleaned in SETTINGS_TRIGGERS_EN
        return cleaned in SETTINGS_TRIGGERS_EN

    def is_greeting(self, message: str, profile: Optional[UserProfile] = None) -> bool:
        """Check if message is a simple greeting/help opener."""
        cleaned = message.strip().lower().rstrip("!?.").strip()
        lang = profile.language if profile else "en"
        if lang == "zh":
            return cleaned in GREETING_TRIGGERS_ZH or cleaned in GREETING_TRIGGERS_EN
        return cleaned in GREETING_TRIGGERS_EN

    def handle(self, phone: str, message: str) -> str:
        """
        Main entry point — route incoming message to onboarding or conversation.

        Args:
            phone:   Sender's WhatsApp number.
            message: Raw message text.

        Returns:
            Fynn's reply string.
        """
        profile, is_new = self.profiles.get_or_create(phone)

        # Settings change request — restart onboarding
        if not is_new and profile.is_onboarding_complete() and self.is_settings_trigger(message, profile):
            profile.onboarding_step = "ask_name"
            self.profiles.save(profile)
            strings = ZH if profile.language == "zh" else EN
            return strings["welcome"]

        # New user or mid-onboarding — run state machine
        if not profile.is_onboarding_complete():
            return self._handle_onboarding(profile, message)

        # Fully onboarded — conversational mode
        return self._handle_conversation(profile, message)

    def clear_history(self, phone: str) -> None:
        """Clear conversation history for a sender."""
        self._history.pop(phone, None)

    # ── Onboarding state machine ───────────────────────────────────────────────

    def _handle_onboarding(self, profile: UserProfile, message: str) -> str:
        """
        Drive the onboarding conversation step by step.

        Args:
            profile: The seller's UserProfile (modified in place).
            message: Current message from the seller.

        Returns:
            Next onboarding prompt or completion message.
        """
        step = profile.onboarding_step
        strings = ZH if profile.language == "zh" else EN
        msg = message.strip()

        # ── Step: welcome / ask name ──
        if step == "ask_name":
            # If this is first contact or a short greeting, show welcome first
            greetings = {"hi", "hello", "hey", "hola", "start", "你好", "开始"}
            if msg.lower() in greetings or not profile.name or profile.name == "Seller":
                # Only treat as name if it looks like a real name (not a greeting)
                if msg.lower() in greetings:
                    return strings["welcome"]
            # Otherwise treat the message as their name
            profile.name = msg.title()
            profile.onboarding_step = "ask_frequency"
            self.profiles.save(profile)
            return strings["ask_frequency"].format(name=profile.name)

        # ── Step: report frequency ──
        elif step == "ask_frequency":
            digits = set(msg.replace(",", " ").split())
            valid = digits & {"1", "2", "3"}
            if not valid:
                return strings["invalid_frequency"]

            profile.daily_enabled = "1" in valid
            profile.weekly_enabled = "2" in valid
            profile.monthly_enabled = "3" in valid
            profile.onboarding_step = "ask_time"
            self.profiles.save(profile)
            return strings["ask_time"]

        # ── Step: report time ──
        elif step == "ask_time":
            try:
                hour = int(msg.replace("am", "").replace("pm", "").replace("AM", "").replace("PM", "").strip())
                # Handle 12-hour format loosely
                if "pm" in msg.lower() and hour != 12:
                    hour += 12
                if not 0 <= hour <= 23:
                    raise ValueError
            except ValueError:
                return strings["invalid_time"]

            profile.report_time_hour = hour
            profile.onboarding_step = "ask_currency"
            self.profiles.save(profile)
            return strings["ask_currency"]

        # ── Step: currency ──
        elif step == "ask_currency":
            currency_map = {"1": "SGD", "2": "MYR", "3": "USD"}
            choice = msg.strip()
            if choice not in currency_map:
                return strings["invalid_currency"]

            profile.currency = currency_map[choice]
            profile.onboarding_step = "ask_language"
            self.profiles.save(profile)
            return strings["ask_language"]

        # ── Step: language ──
        elif step == "ask_language":
            if msg.strip() == "1":
                profile.language = "en"
            elif msg.strip() == "2":
                profile.language = "zh"
            else:
                return strings["invalid_language"]

            profile.onboarding_step = None  # Onboarding complete
            self.profiles.save(profile)

            # Use selected language for completion message
            final_strings = ZH if profile.language == "zh" else EN
            # Store guide message to be sent as follow-up
            self._pending_guide[profile.phone] = (
                GUIDE_ZH if profile.language == "zh" else GUIDE_EN
            )
            return final_strings["complete"].format(
                name=profile.name,
                summary=profile.to_summary(),
            )

        return "Something went wrong — let's start over. What's your name?"

    # ── Conversational Q&A ─────────────────────────────────────────────────────

    def _handle_conversation(self, profile: UserProfile, message: str) -> str:
        """
        Handle a conversational message from a fully onboarded seller.

        Injects P&L context and seller profile into Claude's system prompt.

        Args:
            profile: Seller's UserProfile.
            message: Incoming message text.

        Returns:
            Claude's reply as Fynn.
        """
        if not self.client:
            return "I'm not fully configured yet — check the API key setup."

        phone = profile.phone
        if phone not in self._history:
            self._history[phone] = []

        self._history[phone].append({"role": "user", "content": message})

        base_system = CLAUDE_SYSTEM_ZH if profile.language == "zh" else CLAUDE_SYSTEM_EN
        pnl = self.get_pnl(phone)
        system = base_system
        system += f"\n\nSeller name: {profile.name}. Currency preference: {profile.currency}."
        if pnl:
            system += f"\n\nLatest P&L report:\n{json.dumps(pnl, indent=2)}"
        else:
            system += "\n\nNo P&L report generated yet for this seller."

        recent = self._history[phone][-20:]

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=system,
                messages=recent,
            )
            reply = response.content[0].text.strip()
        except Exception as exc:
            print(f"  [Conversation] Claude error: {exc}")
            reply = "Sorry, I hit a snag — please try again in a moment. 🙏"

        self._history[phone].append({"role": "assistant", "content": reply})
        print(f"  [Conversation] {phone} → {message[:60]}")
        print(f"  [Conversation] Fynn → {reply[:60]}")
        return reply
