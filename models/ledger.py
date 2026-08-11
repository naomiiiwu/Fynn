"""
Chart of accounts and journal entry models for Fynn's internal ledger.

Scope: double-entry journal entries derived from reconciled platform data
and uploaded cost files. No bank-feed matching yet — the debit side of
cash-funded entries posts to a placeholder "unreconciled" cash account
until real bank connectivity exists.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AccountType(str, Enum):
    ASSET = "ASSET"
    REVENUE = "REVENUE"
    CONTRA_REVENUE = "CONTRA_REVENUE"
    EXPENSE = "EXPENSE"
    ADJUSTMENT = "ADJUSTMENT"


class Account(BaseModel):
    code: str
    name: str
    type: AccountType


# Fixed chart of accounts — sufficient for a single-currency, single-entity
# small seller. Not user-editable yet; revisit if/when multi-entity or
# custom categories are needed.
CHART_OF_ACCOUNTS: dict[str, Account] = {
    a.code: a
    for a in [
        Account(code="1000", name="Cash & Bank (unreconciled)", type=AccountType.ASSET),
        Account(code="1100", name="Marketplace Settlement Receivable", type=AccountType.ASSET),
        Account(code="4000", name="Sales Revenue", type=AccountType.REVENUE),
        Account(code="4100", name="Refunds & Returns", type=AccountType.CONTRA_REVENUE),
        Account(code="4200", name="Vouchers & Discounts", type=AccountType.CONTRA_REVENUE),
        Account(code="5000", name="Cost of Goods Sold", type=AccountType.EXPENSE),
        Account(code="6100", name="Platform Fees", type=AccountType.EXPENSE),
        Account(code="6110", name="Shipping Costs", type=AccountType.EXPENSE),
        Account(code="6200", name="Advertising", type=AccountType.EXPENSE),
        Account(code="6300", name="Warehouse & Fulfillment", type=AccountType.EXPENSE),
        Account(code="6400", name="Payroll", type=AccountType.EXPENSE),
        Account(code="6500", name="Packaging", type=AccountType.EXPENSE),
        Account(code="6900", name="Other Business Expenses", type=AccountType.EXPENSE),
        Account(code="9000", name="Reconciliation Variance", type=AccountType.ADJUSTMENT),
    ]
}


class JournalLine(BaseModel):
    account_code: str
    account_name: str
    debit: float = 0.0
    credit: float = 0.0


class JournalEntry(BaseModel):
    entry_id: str
    platform: str
    period: str
    currency: str
    description: str
    lines: list[JournalLine]
    generated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def is_balanced(self, tolerance: float = 0.01) -> bool:
        total_debit = round(sum(l.debit for l in self.lines), 2)
        total_credit = round(sum(l.credit for l in self.lines), 2)
        return abs(total_debit - total_credit) <= tolerance
