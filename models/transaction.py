"""Pydantic data models for Fynn transactions and reports."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TransactionType(str, Enum):
    """Enumeration of transaction types from Shopee."""

    ORDER = "ORDER"
    REFUND = "REFUND"
    PLATFORM_FEE = "PLATFORM_FEE"
    SHIPPING = "SHIPPING"
    VOUCHER = "VOUCHER"
    SETTLEMENT = "SETTLEMENT"
    OTHER = "OTHER"


class Category(str, Enum):
    """AI-assigned accounting categories."""

    REVENUE = "REVENUE"
    PLATFORM_FEE = "PLATFORM_FEE"
    REFUND = "REFUND"
    SHIPPING = "SHIPPING"
    VOUCHER = "VOUCHER"
    OTHER = "OTHER"


class Transaction(BaseModel):
    """A single Shopee transaction entry."""

    transaction_id: str
    order_id: Optional[str] = None
    date: datetime
    type: TransactionType
    description: str
    amount_myr: float = Field(..., description="Amount in Malaysian Ringgit (positive = credit, negative = debit)")
    currency: str = "MYR"
    category: Optional[Category] = None
    confidence_score: Optional[float] = None
    notes: Optional[str] = None


class ReconciliationResult(BaseModel):
    """Result of the payout reconciliation process."""

    source_currency: str = "MYR"    # native currency of the platform CSV (e.g. MYR, SGD, USD)
    gross_sales_myr: float          # amounts are in source_currency despite the _myr suffix
    total_refunds_myr: float
    total_platform_fees_myr: float
    total_shipping_myr: float
    total_vouchers_myr: float
    expected_payout_myr: float
    actual_payout_myr: float
    discrepancy_myr: float
    discrepancy_pct: float
    is_discrepancy_flagged: bool


class Anomaly(BaseModel):
    """A detected anomaly with a human-readable explanation."""

    type: str
    description: str
    severity: str  # "HIGH", "MEDIUM", "LOW"
    related_transaction_id: Optional[str] = None


class PnLReport(BaseModel):
    """Full Profit & Loss report structure."""

    period: str
    platform: str
    currency: str
    revenue: dict
    costs: dict
    profit: dict
    anomalies: list
    generated_at: str
    reconciliation: Optional[dict] = None
    order_count: int = 0
    refund_count: int = 0
