"""Firm profile.

Fynn is sold to accounting firms, not sellers, so the unit of configuration is
the firm: who signs off on a decision, which platforms their clients sell on,
and which ledger the journals are destined for. Rules are scoped to the same
key (services/classification.RuleStore), which is what makes a firm's accumulated
decisions portable across every client they manage.
"""

from dataclasses import dataclass, field
from typing import Optional

SUPPORTED_PLATFORMS = ["Shopee", "Lazada", "TikTok Shop"]
SUPPORTED_LEDGERS = ["dry-run", "xero"]


@dataclass
class FirmProfile:
    """Configuration for one accounting firm, keyed by its WhatsApp number."""

    phone: str                                  # "whatsapp:+6591234567" — the firm_id
    firm: str = "Your firm"                     # firm name, shown on the digest
    actor: str = ""                             # who approves, named in the audit trail
    language: str = "en"                        # "en" or "zh"
    platforms: list[str] = field(default_factory=lambda: list(SUPPORTED_PLATFORMS))
    ledger: str = "dry-run"                     # destination adapter

    # None = setup complete. Anything else means the firm has not finished setup.
    onboarding_step: Optional[str] = "ask_firm"

    def is_onboarding_complete(self) -> bool:
        return self.onboarding_step is None

    def approver(self) -> str:
        """The name recorded against decisions. Never blank — the trail needs one."""
        return self.actor.strip() or self.firm.strip() or "Unnamed approver"

    def to_summary(self) -> str:
        ledger_label = {
            "dry-run": "Dry run (nothing posts)",
            "xero": "Xero (DRAFT manual journals)",
        }.get(self.ledger, self.ledger)
        return (
            f"🏛 Firm: {self.firm}\n"
            f"✍️ Approvals signed by: {self.approver()}\n"
            f"🛒 Platforms: {', '.join(self.platforms) or 'None selected'}\n"
            f"📒 Ledger: {ledger_label}"
        )


class FirmProfileStore:
    """Firm profiles: in-memory cache in front of Supabase.

    Read path:  memory first, then storage on miss.
    Write path: memory + storage, and a storage failure never blocks the reply.
    Fallback:   with Supabase unconfigured this is a pure in-memory store, which
                is what local development runs on.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, FirmProfile] = {}

    def _from_dict(self, data: dict) -> FirmProfile:
        platforms = data.get("platforms") or list(SUPPORTED_PLATFORMS)
        return FirmProfile(
            phone=data["phone"],
            firm=data.get("firm") or "Your firm",
            actor=data.get("actor") or "",
            language=data.get("language", "en"),
            platforms=[p for p in platforms if p in SUPPORTED_PLATFORMS] or list(SUPPORTED_PLATFORMS),
            ledger=data.get("ledger") or "dry-run",
            onboarding_step=data.get("onboarding_step", "ask_firm"),
        )

    def get(self, phone: str) -> Optional[FirmProfile]:
        if phone in self._profiles:
            return self._profiles[phone]

        from services.database import load_firm
        data = load_firm(phone)
        if data:
            profile = self._from_dict(data)
            self._profiles[phone] = profile
            return profile
        return None

    def get_or_create(self, phone: str) -> tuple[FirmProfile, bool]:
        """Return (profile, is_new). is_new=True means setup has not been done."""
        existing = self.get(phone)
        if existing:
            return existing, False
        profile = FirmProfile(phone=phone)
        self._profiles[phone] = profile
        return profile, True

    def save(self, profile: FirmProfile) -> None:
        self._profiles[profile.phone] = profile
        from services.database import save_firm
        save_firm(profile)

    def all(self) -> list[FirmProfile]:
        from services.database import load_all_firms
        for data in load_all_firms():
            phone = data.get("phone")
            if phone and phone not in self._profiles:
                self._profiles[phone] = self._from_dict(data)
        return list(self._profiles.values())
