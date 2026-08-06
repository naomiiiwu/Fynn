"""
Fynn scheduled job service.

Defines three cadences for the bookkeeping pipeline:
  - Daily   : quick WhatsApp ping with today's order count (if any)
  - Weekly  : sends a 7-day rolling summary via WhatsApp
  - Monthly : full P&L report — reconcile, Excel export, WhatsApp summary

None are auto-registered on the scheduler yet — without a live Shopee/Lazada
API connection, a cron-triggered run would just replay whatever manual CSVs
are cached rather than fresh data. Trigger them manually via /run-monthly-report
or /schedule/trigger/{job_id} until real-time ingestion lands.

All times are in the seller's timezone (default: Asia/Kuala_Lumpur).
The daily job, if registered, is meant to run every hour and filter users
whose report_time_hour matches the current local hour — so per-user time
settings are respected.

Schedule env vars:
  SELLER_TIMEZONE          default: Asia/Kuala_Lumpur
  SCHEDULE_WEEKLY_DAY      default: mon (every Monday)
  SCHEDULE_WEEKLY_HOUR     default: 8   (8:00 AM seller time)
  SCHEDULE_MONTHLY_DAY     default: 1   (1st of each month)
  SCHEDULE_MONTHLY_HOUR    default: 8   (8:00 AM seller time)
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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


def _seller_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("SELLER_TIMEZONE", "Asia/Kuala_Lumpur"))


def _now_local() -> datetime:
    return datetime.now(_seller_tz())


async def run_daily_ping() -> None:
    """
    Hourly job: send the daily ping to sellers whose report_time_hour matches
    the current local hour. This lets each user set their own preferred time.
    """
    now_local = _now_local()
    current_hour = now_local.hour
    print(f"\n[Scheduler] ⏰ Daily ping check — {now_local.strftime('%Y-%m-%d %H:%M %Z')}")

    all_daily_profiles = _get_profiles_for_job("daily")

    # Only ping users whose configured hour matches right now (seller local time)
    profiles = [p for p in all_daily_profiles if getattr(p, "report_time_hour", 8) == current_hour]

    # Fallback to env config if no profiles yet
    if not all_daily_profiles:
        fallback_hour = _env_int("SCHEDULE_DAILY_HOUR", 8)
        if current_hour != fallback_hour:
            return  # not the right hour for the fallback
        profiles = [UserProfile(
            phone=os.getenv("TWILIO_WHATSAPP_TO", "").strip(),
            name=_env_str("SELLER_NAME", "Seller"),
            daily_enabled=True,
            onboarding_step=None,
        )]

    if not profiles:
        return  # no one to ping at this hour

    try:
        import main as _main
        from models.transaction import TransactionType

        all_txns = [
            t
            for periods in _main._platform_transactions.values()
            for txns in periods.values()
            for t in txns
        ]
        orders_today = [
            t for t in all_txns
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
            wa = WhatsAppService(to=profile.phone)
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
        import main as _main
        from models.transaction import TransactionType
        from services.currency import CurrencyConverter

        all_txns = [
            t
            for periods in _main._platform_transactions.values()
            for txns in periods.values()
            for t in txns
        ]
        orders = [t for t in all_txns if t.type == TransactionType.ORDER]
        refunds = [t for t in all_txns if t.type == TransactionType.REFUND]
        gross_myr = sum(t.amount_myr for t in orders)

        for profile in profiles:
            currency = getattr(profile, "currency", "MYR")
            if currency == "SGD":
                conv = CurrencyConverter()
                display_amount = conv.myr_to_sgd(gross_myr)["converted_amount"]
            else:
                display_amount = gross_myr

            message = (
                f"Hey {profile.name}! 📊 Your weekly Fynn update:\n\n"
                f"📦 Orders: {len(orders)}\n"
                f"💰 Gross Sales: {currency} {display_amount:,.2f}\n"
                f"↩️ Refunds: {len(refunds)}\n\n"
                f"Full monthly report drops on the 1st.\n"
                f"— Fynn"
            )
            wa = WhatsAppService(to=profile.phone)
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
                periods=[period],
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
    Create and configure the APScheduler instance.
    All times are in SELLER_TIMEZONE (default: Asia/Kuala_Lumpur).

    No jobs are auto-registered yet: without a live Shopee/Lazada API
    connection, a cron-triggered monthly/weekly report would just re-run
    the pipeline against whatever manual CSVs happen to be cached, not
    fresh data. run_monthly_report/run_weekly_summary/run_daily_ping stay
    available for manual triggering (see /run-monthly-report and
    /schedule/trigger/{job_id}) until real-time ingestion is wired up.
    """
    tz = _seller_tz()
    scheduler = AsyncIOScheduler(timezone=tz)
    return scheduler
