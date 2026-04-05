"""
Fynn scheduled job service.

Runs the bookkeeping pipeline automatically on three cadences:
  - Daily   : quick WhatsApp ping with today's order count (if any)
  - Weekly  : sends a 7-day rolling summary via WhatsApp
  - Monthly : full P&L report — reconcile, Sheets, WhatsApp summary

All schedules are configurable via .env so you can adjust timing
without touching code.

Schedule env vars (24h format, server local time):
  SCHEDULE_DAILY_HOUR      default: 8   (8:00 AM every day)
  SCHEDULE_WEEKLY_DAY      default: mon (every Monday)
  SCHEDULE_WEEKLY_HOUR     default: 8   (8:00 AM)
  SCHEDULE_MONTHLY_DAY     default: 1   (1st of each month)
  SCHEDULE_MONTHLY_HOUR    default: 8   (8:00 AM)
"""

import os
from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.orchestrator import OrchestratorAgent
from models.user_profile import ProfileStore, UserProfile
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message

# Shared profile store — injected by main.py after app starts
_profile_store: ProfileStore | None = None


def set_profile_store(store: ProfileStore) -> None:
    """
    Inject the shared ProfileStore so scheduler jobs can read per-user settings.

    Args:
        store: The ProfileStore instance from ConversationManager.
    """
    global _profile_store
    _profile_store = store


def _env_int(key: str, default: int) -> int:
    """Read an integer env var with a fallback default."""
    try:
        return int(os.getenv(key, str(default)).strip())
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    """Read a string env var with a fallback default."""
    return os.getenv(key, default).strip()


# ── Job functions ───────────────────────────────────────────────────────────────

def _get_profiles_for_job(job_type: str) -> list[UserProfile]:
    """
    Return all onboarded profiles that have opted into a given job type.

    Args:
        job_type: "daily", "weekly", or "monthly"

    Returns:
        List of matching UserProfile objects.
    """
    if _profile_store is None:
        return []
    all_profiles = _profile_store.all()
    enabled_attr = f"{job_type}_enabled"
    return [
        p for p in all_profiles
        if p.is_onboarding_complete() and getattr(p, enabled_attr, False)
    ]


async def run_daily_ping() -> None:
    """
    Daily job: send a short WhatsApp ping to each seller who enabled daily reports.

    Loads today's transactions and sends a brief update per seller.
    Falls back to global .env config if no profiles exist yet.
    """
    print(f"\n[Scheduler] ⏰ Daily ping — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    profiles = _get_profiles_for_job("daily")

    # Fallback to env config if no profiles yet
    if not profiles:
        profiles = [UserProfile(
            phone=os.getenv("TWILIO_WHATSAPP_TO", "").strip(),
            name=_env_str("SELLER_NAME", "Seller"),
            daily_enabled=True,
            onboarding_step=None,
        )]

    try:
        from data.mock_shopee_data import get_mock_transactions
        from models.transaction import TransactionType

        txns = get_mock_transactions()
        orders_today = [
            t for t in txns
            if t.type == TransactionType.ORDER
            and t.date.day == datetime.now().day
        ]
        total = sum(t.amount_myr for t in orders_today)

        for profile in profiles:
            if orders_today:
                message = (
                    f"Good morning {profile.name}! ☀️\n\n"
                    f"📦 Today so far: {len(orders_today)} order{'s' if len(orders_today) != 1 else ''} "
                    f"(MYR {total:,.2f})\n\n"
                    f"— Fynn"
                )
            else:
                message = (
                    f"Good morning {profile.name}! ☀️\n\n"
                    f"No new orders yet today. I'll keep watching.\n\n"
                    f"— Fynn"
                )
            wa = WhatsAppService()
            wa.send(message)
            print(f"  [Scheduler] Daily ping sent to {profile.phone}.")

    except Exception as exc:
        print(f"  [Scheduler] Daily ping failed: {exc}")


async def run_weekly_summary() -> None:
    """
    Weekly job: send a 7-day rolling summary to each seller who enabled weekly reports.
    """
    print(f"\n[Scheduler] ⏰ Weekly summary — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    profiles = _get_profiles_for_job("weekly")
    if not profiles:
        profiles = [UserProfile(
            phone=os.getenv("TWILIO_WHATSAPP_TO", "").strip(),
            name=_env_str("SELLER_NAME", "Seller"),
            weekly_enabled=True,
            onboarding_step=None,
        )]

    try:
        from data.mock_shopee_data import get_mock_transactions
        from models.transaction import TransactionType
        from services.currency import CurrencyConverter

        txns = get_mock_transactions()
        orders = [t for t in txns if t.type == TransactionType.ORDER]
        refunds = [t for t in txns if t.type == TransactionType.REFUND]
        gross_myr = sum(t.amount_myr for t in orders)
        conv = CurrencyConverter()
        gross_sgd = conv.myr_to_sgd(gross_myr)["converted_amount"]

        for profile in profiles:
            message = (
                f"Hey {profile.name}! 📊 Your weekly Fynn update:\n\n"
                f"📦 Orders: {len(orders)}\n"
                f"💰 Gross Sales: {profile.currency} {gross_sgd:,.2f}\n"
                f"↩️ Refunds: {len(refunds)}\n\n"
                f"Full monthly report drops on the 1st.\n"
                f"— Fynn"
            )
            wa = WhatsAppService()
            wa.send(message)
            print(f"  [Scheduler] Weekly summary sent to {profile.phone}.")

    except Exception as exc:
        print(f"  [Scheduler] Weekly summary failed: {exc}")


async def run_monthly_report() -> None:
    """
    Monthly job: run the full 7-step agent pipeline for each seller who enabled monthly reports.
    """
    print(f"\n[Scheduler] ⏰ Monthly report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    profiles = _get_profiles_for_job("monthly")
    if not profiles:
        profiles = [UserProfile(
            phone=os.getenv("TWILIO_WHATSAPP_TO", "").strip(),
            name=_env_str("SELLER_NAME", "Seller"),
            monthly_enabled=True,
            onboarding_step=None,
        )]

    now = datetime.now()
    period = now.strftime("%B %Y")

    for profile in profiles:
        try:
            import main as _main
            platform_list = list(_main._platform_transactions.keys()) or ["shopee"]
            result = OrchestratorAgent().run_sync(
                sender=profile.phone,
                period=period,
                platform_list=platform_list,
                profile=profile,
            )
            profit = result.combined_pnl.get("profit", {}).get("net_profit", 0) if result.combined_pnl else 0
            print(f"  [Scheduler] Monthly report complete for {profile.name}. "
                  f"Net profit: {profile.currency} {profit:,.2f}")
        except Exception as exc:
            print(f"  [Scheduler] Monthly report failed for {profile.phone}: {exc}")


# ── Scheduler factory ───────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    """
    Create and configure the APScheduler instance with all three jobs.

    Schedule is read from environment variables so it can be tuned
    without code changes. Call scheduler.start() to activate.

    Returns:
        Configured AsyncIOScheduler (not yet started).
    """
    scheduler = AsyncIOScheduler()

    # Daily ping
    daily_hour = _env_int("SCHEDULE_DAILY_HOUR", 8)
    scheduler.add_job(
        run_daily_ping,
        trigger=CronTrigger(hour=daily_hour, minute=0),
        id="daily_ping",
        name="Daily order ping",
        replace_existing=True,
    )
    print(f"  [Scheduler] Daily ping scheduled at {daily_hour:02d}:00 every day.")

    # Weekly summary
    weekly_day = _env_str("SCHEDULE_WEEKLY_DAY", "mon")
    weekly_hour = _env_int("SCHEDULE_WEEKLY_HOUR", 8)
    scheduler.add_job(
        run_weekly_summary,
        trigger=CronTrigger(day_of_week=weekly_day, hour=weekly_hour, minute=0),
        id="weekly_summary",
        name="Weekly P&L summary",
        replace_existing=True,
    )
    print(f"  [Scheduler] Weekly summary scheduled at {weekly_hour:02d}:00 every {weekly_day.capitalize()}.")

    # Monthly full report
    monthly_day = _env_int("SCHEDULE_MONTHLY_DAY", 1)
    monthly_hour = _env_int("SCHEDULE_MONTHLY_HOUR", 8)
    scheduler.add_job(
        run_monthly_report,
        trigger=CronTrigger(day=monthly_day, hour=monthly_hour, minute=0),
        id="monthly_report",
        name="Full monthly P&L report",
        replace_existing=True,
    )
    print(f"  [Scheduler] Monthly report scheduled at {monthly_hour:02d}:00 on day {monthly_day} of each month.")

    return scheduler
