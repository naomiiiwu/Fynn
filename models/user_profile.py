"""
User profile model for Fynn sellers.

Stores per-seller configuration collected during onboarding.
In-memory for MVP — replace the store with Supabase in v1.5.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UserProfile:
    """
    All configurable settings for a single seller.

    Populated step by step during the WhatsApp onboarding flow.
    Persisted in memory keyed by the seller's WhatsApp phone number.
    """

    phone: str                          # WhatsApp number, e.g. "whatsapp:+6591234567"
    name: str = "Seller"               # Personalisation name
    language: str = "en"               # "en" or "zh"
    currency: str = "SGD"              # Display currency: SGD, MYR, USD
    report_time_hour: int = 8          # Hour to send scheduled messages (0-23)

    # Which report cadences are enabled
    daily_enabled: bool = False
    weekly_enabled: bool = True
    monthly_enabled: bool = True

    # Weekly day: mon, tue, wed, thu, fri, sat, sun
    weekly_day: str = "mon"

    # Monthly day of month (1-28)
    monthly_day: int = 1

    # Anomaly sensitivity: "strict", "normal", "relaxed"
    anomaly_sensitivity: str = "normal"

    # Onboarding state machine step
    # None = onboarding complete, otherwise tracks current step
    onboarding_step: Optional[str] = "ask_name"

    # Partial selections during onboarding (temp storage)
    _pending_frequencies: list = field(default_factory=list)

    def is_onboarding_complete(self) -> bool:
        """Return True if the seller has finished onboarding."""
        return self.onboarding_step is None

    def to_summary(self) -> str:
        """
        Return a human-readable settings summary for WhatsApp.

        Returns:
            Multi-line string of current configuration.
        """
        lang_label = "English" if self.language == "en" else "中文"
        cadences = []
        if self.daily_enabled:
            cadences.append(f"Daily ping ({self.report_time_hour:02d}:00)")
        if self.weekly_enabled:
            cadences.append(f"Weekly summary ({self.weekly_day.capitalize()} {self.report_time_hour:02d}:00)")
        if self.monthly_enabled:
            cadences.append(f"Monthly P&L (Day {self.monthly_day}, {self.report_time_hour:02d}:00)")

        cadence_str = "\n  • ".join(cadences) if cadences else "None"

        sensitivity_label = {
            "strict": "Strict (flag anything unusual)",
            "normal": "Normal (default)",
            "relaxed": "Relaxed (major issues only)",
        }.get(self.anomaly_sensitivity, "Normal")

        return (
            f"👤 Name: {self.name}\n"
            f"🌐 Language: {lang_label}\n"
            f"💱 Currency: {self.currency}\n"
            f"📅 Reports:\n  • {cadence_str}\n"
            f"🔔 Anomaly alerts: {sensitivity_label}"
        )


# ── In-memory profile store ────────────────────────────────────────────────────

class ProfileStore:
    """
    Simple in-memory store for UserProfile objects keyed by phone number.

    Replace with Supabase calls in v1.5 for persistence across restarts.
    """

    def __init__(self) -> None:
        """Initialise empty store."""
        self._profiles: dict[str, UserProfile] = {}

    def get(self, phone: str) -> Optional[UserProfile]:
        """
        Retrieve a profile by phone number.

        Args:
            phone: WhatsApp number string.

        Returns:
            UserProfile or None if not found.
        """
        return self._profiles.get(phone)

    def get_or_create(self, phone: str) -> tuple[UserProfile, bool]:
        """
        Get existing profile or create a new one for onboarding.

        Args:
            phone: WhatsApp number string.

        Returns:
            Tuple of (UserProfile, is_new). is_new=True means onboarding needed.
        """
        if phone in self._profiles:
            return self._profiles[phone], False
        profile = UserProfile(phone=phone)
        self._profiles[phone] = profile
        return profile, True

    def save(self, profile: UserProfile) -> None:
        """
        Persist a profile (in-memory for now).

        Args:
            profile: The UserProfile to save.
        """
        self._profiles[profile.phone] = profile

    def all(self) -> list[UserProfile]:
        """Return all stored profiles."""
        return list(self._profiles.values())
