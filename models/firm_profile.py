"""Firm profile.

Fynn is sold to accounting firms, not sellers, so the unit of configuration is
the firm: who signs off on a decision, which platforms their clients sell on,
and which ledger the journals are destined for. Rules are scoped to the same
key (services/classification.RuleStore), which is what makes a firm's
accumulated decisions portable across every client they manage.

One workspace per deployment for now. The firm used to be identified by the
WhatsApp number that messaged Fynn; with the interface on the web and no
accounts yet, there is exactly one, under a fixed id. Everything downstream
already takes a firm id, so multi-firm is an authentication problem rather than
an engine one.
"""

from dataclasses import dataclass, field
from typing import Optional

WORKSPACE_ID = "workspace"

SUPPORTED_PLATFORMS = ["Shopee", "Lazada", "TikTok Shop"]
SUPPORTED_LEDGERS = ["dry-run", "xero"]


@dataclass
class FirmProfile:
    """Configuration for one accounting firm."""

    id: str = WORKSPACE_ID
    firm: str = "Your firm"                     # firm name, shown on the digest
    actor: str = ""                             # who approves, named in the audit trail
    platforms: list[str] = field(default_factory=lambda: list(SUPPORTED_PLATFORMS))
    ledger: str = "dry-run"                     # destination adapter

    # None = setup complete. Anything else means the firm has not finished setup.
    onboarding_step: Optional[str] = "ask_firm"

    def is_onboarding_complete(self) -> bool:
        return self.onboarding_step is None

    def approver(self) -> str:
        """The name recorded against decisions. Never blank — the trail needs one."""
        return self.actor.strip() or self.firm.strip() or "Unnamed approver"

    def to_dict(self) -> dict:
        return {
            "firm": self.firm,
            "actor": self.actor,
            "platforms": list(self.platforms),
            "ledger": self.ledger,
            "complete": self.is_onboarding_complete(),
        }


class FirmProfileStore:
    """The workspace's profile: in-memory, written through to storage.

    With Supabase unconfigured this is a pure in-memory store, which is what
    local development runs on.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, FirmProfile] = {}

    def _from_dict(self, data: dict) -> FirmProfile:
        platforms = data.get("platforms") or list(SUPPORTED_PLATFORMS)
        return FirmProfile(
            id=data.get("id") or WORKSPACE_ID,
            firm=data.get("firm") or "Your firm",
            actor=data.get("actor") or "",
            platforms=[p for p in platforms if p in SUPPORTED_PLATFORMS] or list(SUPPORTED_PLATFORMS),
            ledger=data.get("ledger") or "dry-run",
            onboarding_step=data.get("onboarding_step", "ask_firm"),
        )

    def get(self, firm_id: str = WORKSPACE_ID) -> Optional[FirmProfile]:
        if firm_id in self._profiles:
            return self._profiles[firm_id]

        from services.database import load_firm
        data = load_firm(firm_id)
        if data:
            profile = self._from_dict(data)
            self._profiles[firm_id] = profile
            return profile
        return None

    def get_or_create(self, firm_id: str = WORKSPACE_ID) -> tuple[FirmProfile, bool]:
        """Return (profile, is_new). is_new=True means setup has not been done."""
        existing = self.get(firm_id)
        if existing:
            return existing, False
        profile = FirmProfile(id=firm_id)
        self._profiles[firm_id] = profile
        return profile, True

    def save(self, profile: FirmProfile) -> None:
        self._profiles[profile.id] = profile
        from services.database import save_firm
        save_firm(profile)
