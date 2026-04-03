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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.bookkeeper import BookkeeperAgent
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message


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

async def run_daily_ping() -> None:
    """
    Daily job: send a short WhatsApp ping to the seller.

    Loads today's transactions from mock data and sends a
    brief update. In production this would query today's
    real orders from the platform API.
    """
    print(f"\n[Scheduler] ⏰ Daily ping — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

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

        seller_name = _env_str("SELLER_NAME", "Seller")
        if orders_today:
            message = (
                f"Good morning {seller_name}! ☀️\n\n"
                f"📦 Today so far: {len(orders_today)} order{'s' if len(orders_today) != 1 else ''} "
                f"(MYR {total:,.2f})\n\n"
                f"— Fynn"
            )
        else:
            message = (
                f"Good morning {seller_name}! ☀️\n\n"
                f"No new orders yet today. I'll keep watching.\n\n"
                f"— Fynn"
            )

        wa = WhatsAppService()
        wa.send(message)
        print(f"  [Scheduler] Daily ping sent.")

    except Exception as exc:
        print(f"  [Scheduler] Daily ping failed: {exc}")


async def run_weekly_summary() -> None:
    """
    Weekly job: send a 7-day rolling summary via WhatsApp.

    Runs a condensed version of the pipeline for the past week
    and delivers a brief summary — not the full P&L.
    """
    print(f"\n[Scheduler] ⏰ Weekly summary — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    try:
        from data.mock_shopee_data import get_mock_transactions
        from models.transaction import TransactionType
        from services.currency import CurrencyConverter

        txns = get_mock_transactions()
        orders = [t for t in txns if t.type == TransactionType.ORDER]
        refunds = [t for t in txns if t.type == TransactionType.REFUND]
        gross_myr = sum(t.amount_myr for t in orders)

        conv = CurrencyConverter()
        result = conv.myr_to_sgd(gross_myr)
        gross_sgd = result["converted_amount"]

        seller_name = _env_str("SELLER_NAME", "Seller")
        message = (
            f"Hey {seller_name}! 📊 Your weekly Fynn update:\n\n"
            f"📦 Orders: {len(orders)}\n"
            f"💰 Gross Sales: SGD {gross_sgd:,.2f}\n"
            f"↩️ Refunds: {len(refunds)}\n\n"
            f"Full monthly report drops on the 1st.\n"
            f"— Fynn"
        )

        wa = WhatsAppService()
        wa.send(message)
        print(f"  [Scheduler] Weekly summary sent.")

    except Exception as exc:
        print(f"  [Scheduler] Weekly summary failed: {exc}")


async def run_monthly_report() -> None:
    """
    Monthly job: run the full 7-step agent pipeline.

    This is the same as POST /run-monthly-report — reconciles
    payouts, generates P&L, writes to Sheets, sends WhatsApp.
    """
    print(f"\n[Scheduler] ⏰ Monthly report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    try:
        # Determine period label from current month
        now = datetime.now()
        period = now.strftime("%B %Y")  # e.g. "April 2026"

        agent = BookkeeperAgent()
        result = agent.run(period=period, platform="Shopee MY")

        pnl = result.get("pnl", {})
        print(f"  [Scheduler] Monthly report complete. Net profit: SGD {pnl.get('profit', {}).get('net_profit', 0):,.2f}")

    except Exception as exc:
        print(f"  [Scheduler] Monthly report failed: {exc}")


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
