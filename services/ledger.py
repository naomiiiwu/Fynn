"""
Ledger service — turns a reconciled platform period + cost totals into
balanced double-entry journal entries.

Two entries per (platform, period):
  1. Marketplace settlement — revenue, refunds, platform-side costs, and
     the actual settlement, with any reconciliation discrepancy absorbed
     by the Reconciliation Variance account so the entry always balances.
  2. Business expenses — COGS/ads/warehouse/payroll/packaging/other,
     funded from the unreconciled cash account (no bank feed yet).

Pure math — no Claude call, mirrors services/reconciliation.py.
"""

import uuid

from models.ledger import CHART_OF_ACCOUNTS, JournalEntry, JournalLine
from models.transaction import ReconciliationResult

_A = {code: acc.name for code, acc in CHART_OF_ACCOUNTS.items()}


def _line(code: str, debit: float = 0.0, credit: float = 0.0) -> JournalLine:
    return JournalLine(account_code=code, account_name=_A[code], debit=round(debit, 2), credit=round(credit, 2))


def build_settlement_entry(
    reconciliation: ReconciliationResult,
    platform: str,
    period: str,
) -> JournalEntry:
    """Build the marketplace settlement journal entry for one platform/period."""
    lines: list[JournalLine] = [
        _line("1100", debit=reconciliation.actual_payout_myr),
        _line("4100", debit=reconciliation.total_refunds_myr),
        _line("6100", debit=reconciliation.total_platform_fees_myr),
        _line("4200", debit=reconciliation.total_vouchers_myr),
        _line("4000", credit=reconciliation.gross_sales_myr),
    ]

    # Net shipping can be a cost (debit) or a net rebate (credit) depending on sign.
    shipping = reconciliation.total_shipping_myr
    if shipping >= 0:
        lines.append(_line("6110", debit=shipping))
    else:
        lines.append(_line("6110", credit=-shipping))

    # Plug the platform's own discrepancy (actual vs. expected payout) so the
    # entry balances without hiding the mismatch — it lands in a variance
    # account instead of silently distorting revenue or costs.
    total_debit = round(sum(l.debit for l in lines), 2)
    total_credit = round(sum(l.credit for l in lines), 2)
    diff = round(total_credit - total_debit, 2)
    if diff > 0:
        lines.append(_line("9000", debit=diff))
    elif diff < 0:
        lines.append(_line("9000", credit=-diff))

    entry = JournalEntry(
        entry_id=str(uuid.uuid4()),
        platform=platform,
        period=period,
        currency=reconciliation.source_currency,
        description=f"Marketplace settlement — {platform} {period}",
        lines=lines,
    )
    assert entry.is_balanced(), f"Settlement entry for {platform}/{period} does not balance: {lines}"
    return entry


_COST_ACCOUNT_CODES = {
    "cogs": "5000",
    "ads": "6200",
    "warehouse": "6300",
    "payroll": "6400",
    "packaging": "6500",
    "expense": "6900",
}


def build_expense_entry(
    cost_totals: dict[str, float],
    platform: str,
    period: str,
    currency: str,
) -> JournalEntry | None:
    """Build the business-expense journal entry, funded from unreconciled cash."""
    lines: list[JournalLine] = []
    total = 0.0
    for key, code in _COST_ACCOUNT_CODES.items():
        amount = cost_totals.get(key, 0.0)
        if amount:
            lines.append(_line(code, debit=amount))
            total += amount

    if not lines:
        return None

    lines.append(_line("1000", credit=total))

    entry = JournalEntry(
        entry_id=str(uuid.uuid4()),
        platform=platform,
        period=period,
        currency=currency,
        description=f"Business expenses — {platform} {period}",
        lines=lines,
    )
    assert entry.is_balanced(), f"Expense entry for {platform}/{period} does not balance: {lines}"
    return entry


def build_journal_entries(
    reconciliation: ReconciliationResult,
    cost_totals: dict[str, float],
    platform: str,
    period: str,
) -> list[JournalEntry]:
    """Build all journal entries for one platform/period. Always balanced."""
    entries = [build_settlement_entry(reconciliation, platform, period)]
    expense_entry = build_expense_entry(cost_totals, platform, period, reconciliation.source_currency)
    if expense_entry:
        entries.append(expense_entry)
    return entries
