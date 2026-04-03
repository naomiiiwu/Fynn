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


# ── Profile store (in-memory + Supabase) ──────────────────────────────────────

class ProfileStore:
    """
    Profile store backed by in-memory cache + Supabase persistence.

    Read path:  memory first, then Supabase on miss.
    Write path: memory + Supabase (fire-and-forget, never blocks).
    Fallback:   if Supabase is unavailable, behaves as pure in-memory store.
    """

    def __init__(self) -> None:
        """Initialise empty in-memory cache."""
        self._profiles: dict[str, UserProfile] = {}

    def _from_dict(self, data: dict) -> UserProfile:
        """
        Convert a Supabase row dict to a UserProfile.

        Args:
            data: Raw dict from Supabase.

        Returns:
            UserProfile instance.
        """
        return UserProfile(
            phone=data["phone"],
            name=data.get("name", "Seller"),
            language=data.get("language", "en"),
            currency=data.get("currency", "SGD"),
            report_time_hour=data.get("report_time_hour", 8),
            daily_enabled=data.get("daily_enabled", False),
            weekly_enabled=data.get("weekly_enabled", True),
            monthly_enabled=data.get("monthly_enabled", True),
            weekly_day=data.get("weekly_day", "mon"),
            monthly_day=data.get("monthly_day", 1),
            anomaly_sensitivity=data.get("anomaly_sensitivity", "normal"),
            onboarding_step=data.get("onboarding_step", "ask_name"),
        )

    def get(self, phone: str) -> Optional[UserProfile]:
        """
        Retrieve a profile — memory first, then Supabase.

        Args:
            phone: WhatsApp number string.

        Returns:
            UserProfile or None if not found anywhere.
        """
        if phone in self._profiles:
            return self._profiles[phone]

        # Try Supabase
        from services.database import load_profile
        data = load_profile(phone)
        if data:
            profile = self._from_dict(data)
            self._profiles[phone] = profile
            return profile

        return None

    def get_or_create(self, phone: str) -> tuple[UserProfile, bool]:
        """
        Get existing profile or create a new one for onboarding.

        Args:
            phone: WhatsApp number string.

        Returns:
            Tuple of (UserProfile, is_new). is_new=True means onboarding needed.
        """
        existing = self.get(phone)
        if existing:
            return existing, False

        profile = UserProfile(phone=phone)
        self._profiles[phone] = profile
        return profile, True

    def save(self, profile: UserProfile) -> None:
        """
        Save profile to memory and persist to Supabase.

        Args:
            profile: The UserProfile to save.
        """
        self._profiles[profile.phone] = profile
        from services.database import save_profile
        save_profile(profile)

    def all(self) -> list[UserProfile]:
        """
        Return all profiles — merges Supabase records with in-memory cache.

        Returns:
            List of all known UserProfile objects.
        """
        from services.database import load_all_profiles
        db_profiles = load_all_profiles()
        for data in db_profiles:
            phone = data["phone"]
            if phone not in self._profiles:
                self._profiles[phone] = self._from_dict(data)
        return list(self._profiles.values())
