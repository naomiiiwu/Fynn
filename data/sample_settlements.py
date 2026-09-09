"""Sample settlement data for cycle 2026-01.

The edge cases here are the ones practising accountants named as the hard part:
later-cycle refunds, unrecognised fee labels, and withheld balances. Each
platform is constructed so the arithmetic only ties out once the exception is
resolved — that is the behaviour worth demonstrating.
"""
from models.transaction import Platform, SettlementLine

CYCLE = "2026-01"
PRIOR_CYCLE = "2025-12"


def _line(platform, label, amount, order_id=None, date=None, cycle=CYCLE):
    return SettlementLine(
        platform=platform, cycle=cycle, label=label, amount=amount,
        order_id=order_id, date=date, source_ref=f"{platform.value}-{cycle}.csv",
    )


def sample_lines() -> list[SettlementLine]:
    lines: list[SettlementLine] = []

    # Lazada — refund belongs to an order from the previous cycle.
    for oid, amt in [("#L901", 420.00), ("#L902", 380.00), ("#L903", 400.00)]:
        lines.append(_line(Platform.LAZADA, "Sale", amt, oid, "2026-01-08"))
    lines.append(_line(Platform.LAZADA, "Commission", -120.00, date="2026-01-31"))
    lines.append(_line(Platform.LAZADA, "Voucher subsidy", -30.00, date="2026-01-31"))
    lines.append(_line(Platform.LAZADA, "Refund", -12.00, "#4521", "2026-01-14"))

    # Shopee — a fee label this firm has not seen before.
    for oid, amt in [("#S220", 500.00), ("#S221", 350.00)]:
        lines.append(_line(Platform.SHOPEE, "Sale", amt, oid, "2026-01-05"))
    lines.append(_line(Platform.SHOPEE, "Commission", -85.00, date="2026-01-31"))
    lines.append(_line(Platform.SHOPEE, "Refund", -31.60, "#S220", "2026-01-19"))
    lines.append(_line(Platform.SHOPEE, "Creator commission", -8.40, date="2026-01-22"))

    # TikTok Shop — settled but not released.
    for oid, amt in [("#T110", 480.00), ("#T111", 320.00)]:
        lines.append(_line(Platform.TIKTOK, "Sale", amt, oid, "2026-01-11"))
    lines.append(_line(Platform.TIKTOK, "Commission", -80.00, date="2026-01-31"))
    lines.append(_line(Platform.TIKTOK, "Payment fee", -12.00, date="2026-01-31"))
    lines.append(_line(Platform.TIKTOK, "Withheld balance", -96.00, date="2026-01-31"))

    return lines


def prior_cycle_lines() -> list[SettlementLine]:
    """Earlier cycles, searched when a refund has no matching sale."""
    return [
        _line(Platform.LAZADA, "Sale", 12.00, "#4521", "2025-12-19", cycle=PRIOR_CYCLE),
    ]


# What each platform said it paid out. Fynn checks its classified total against
# these figures — never against bank data.
REPORTED_PAYOUTS = {
    Platform.LAZADA: 1038.00,
    Platform.SHOPEE: 725.00,
    Platform.TIKTOK: 612.00,
}
