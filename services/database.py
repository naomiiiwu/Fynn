"""
Supabase database service for Fynn.

Handles all persistence:
  - seller_profiles  : onboarding settings per WhatsApp number
  - pnl_reports      : generated P&L reports per seller

Designed with a graceful fallback — if Supabase is not configured,
all operations silently no-op and the app continues with in-memory state.
This means local dev works with zero Supabase setup.
"""

import json
import os
from typing import Optional

_client = None


def _get_client():
    """
    Lazily initialise the Supabase client.

    Returns:
        Supabase client or None if credentials are missing.
    """
    global _client
    if _client is not None:
        return _client

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        print("  [DB] Supabase connected.")
        return _client
    except Exception as exc:
        print(f"  [DB] Supabase connection failed: {exc}")
        return None


# ── Seller profiles ────────────────────────────────────────────────────────────

def save_profile(profile) -> bool:
    """
    Upsert a seller profile to Supabase.

    Args:
        profile: UserProfile dataclass instance.

    Returns:
        True if saved, False if Supabase unavailable.
    """
    client = _get_client()
    if not client:
        return False

    try:
        data = {
            "phone":               profile.phone,
            "name":                profile.name,
            "language":            profile.language,
            "currency":            profile.currency,
            "report_time_hour":    profile.report_time_hour,
            "daily_enabled":       profile.daily_enabled,
            "weekly_enabled":      profile.weekly_enabled,
            "monthly_enabled":     profile.monthly_enabled,
            "weekly_day":          profile.weekly_day,
            "monthly_day":         profile.monthly_day,
            "anomaly_sensitivity": profile.anomaly_sensitivity,
            "onboarding_step":     profile.onboarding_step,
        }
        client.table("seller_profiles").upsert(data).execute()
        print(f"  [DB] Profile saved for {profile.phone}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save profile: {exc}")
        return False


def load_profile(phone: str) -> Optional[dict]:
    """
    Load a seller profile from Supabase by phone number.

    Args:
        phone: WhatsApp number string.

    Returns:
        Profile dict or None if not found.
    """
    client = _get_client()
    if not client:
        return None

    try:
        result = client.table("seller_profiles").select("*").eq("phone", phone).execute()
        if result.data:
            return result.data[0]
        return None
    except Exception as exc:
        print(f"  [DB] Failed to load profile: {exc}")
        return None


def load_all_profiles() -> list[dict]:
    """
    Load all seller profiles from Supabase.

    Returns:
        List of profile dicts, empty list on failure.
    """
    client = _get_client()
    if not client:
        return []

    try:
        result = client.table("seller_profiles").select("*").execute()
        return result.data or []
    except Exception as exc:
        print(f"  [DB] Failed to load profiles: {exc}")
        return []


# ── P&L reports ────────────────────────────────────────────────────────────────

def save_pnl(phone: str, pnl: dict) -> bool:
    """
    Insert a P&L report for a seller.

    Args:
        phone: WhatsApp number string.
        pnl:   Full P&L dict from formatter.generate_pnl().

    Returns:
        True if saved, False if Supabase unavailable.
    """
    client = _get_client()
    if not client:
        return False

    try:
        client.table("pnl_reports").insert({
            "phone":       phone,
            "period":      pnl.get("period", ""),
            "platform":    pnl.get("platform", "Shopee MY"),
            "report_data": json.dumps(pnl),
        }).execute()
        print(f"  [DB] P&L saved for {phone} — {pnl.get('period')}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save P&L: {exc}")
        return False


def load_latest_pnl(phone: str) -> Optional[dict]:
    """
    Load the most recent P&L report for a seller.

    Args:
        phone: WhatsApp number string.

    Returns:
        P&L dict or None if not found.
    """
    client = _get_client()
    if not client:
        return None

    try:
        result = (
            client.table("pnl_reports")
            .select("report_data")
            .eq("phone", phone)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return json.loads(result.data[0]["report_data"])
        return None
    except Exception as exc:
        print(f"  [DB] Failed to load P&L: {exc}")
        return None


# ── CSV uploads ────────────────────────────────────────────────────────────────

def save_csv(csv_bytes: bytes, period: str) -> bool:
    """
    Persist uploaded CSV raw bytes to Supabase so it survives restarts.

    Args:
        csv_bytes: Raw CSV file content.
        period:    Period label e.g. "March 2026".

    Returns:
        True if saved, False if unavailable.
    """
    client = _get_client()
    if not client:
        return False
    try:
        client.table("csv_uploads").upsert({
            "id": "latest",
            "period": period,
            "csv_data": csv_bytes.decode("utf-8-sig", errors="replace"),
        }).execute()
        print(f"  [DB] CSV saved for period {period}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save CSV: {exc}")
        return False


def load_latest_csv() -> Optional[tuple[bytes, str]]:
    """
    Load the most recently uploaded CSV from Supabase.

    Returns:
        Tuple of (csv_bytes, period) or None if not found.
    """
    client = _get_client()
    if not client:
        return None
    try:
        result = client.table("csv_uploads").select("csv_data,period").eq("id", "latest").execute()
        if result.data:
            row = result.data[0]
            return row["csv_data"].encode("utf-8"), row["period"]
        return None
    except Exception as exc:
        print(f"  [DB] Failed to load CSV: {exc}")
        return None


def load_latest_pnl_any() -> Optional[dict]:
    """
    Load the most recent P&L report across all sellers.

    Used to restore _last_pnl on server restart.

    Returns:
        P&L dict or None if no reports exist.
    """
    client = _get_client()
    if not client:
        return None

    try:
        result = (
            client.table("pnl_reports")
            .select("report_data")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return json.loads(result.data[0]["report_data"])
        return None
    except Exception as exc:
        print(f"  [DB] Failed to load latest P&L: {exc}")
        return None
