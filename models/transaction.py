"""Data models for settlement reconciliation."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Platform(str, Enum):
    SHOPEE = "Shopee"
    LAZADA = "Lazada"
    TIKTOK = "TikTok Shop"


class Side(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class SettlementLine(BaseModel):
    """One line from a platform settlement report."""

    platform: Platform
    cycle: str                      # e.g. "2026-01"
    label: str                      # the platform's own fee/line label
    amount: float                   # signed: positive = money in, negative = deduction
    order_id: Optional[str] = None
    date: Optional[str] = None
    source_ref: Optional[str] = None  # settlement file this came from

    @property
    def key(self) -> str:
        return f"{self.platform.value}|{self.cycle}|{self.label}|{self.order_id or '-'}"


class Rule(BaseModel):
    """A firm's decision about how a line label is treated.

    This is the accumulating asset: every exception a firm resolves becomes a
    rule, applied consistently across all their clients from then on.
    """

    platform: Optional[Platform] = None   # None = applies to all platforms
    label: str
    account: str
    side: Side
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None

    def matches(self, line: SettlementLine) -> bool:
        if self.platform is not None and self.platform != line.platform:
            return False
        return self.label.strip().lower() == line.label.strip().lower()


ExceptionKind = Literal[
    "unknown_label",      # no rule exists for this label
    "orphan_refund",      # refund references an order not in this cycle
    "withheld_balance",   # settled but not released by the platform
    "partial_refund",     # refund is a fraction of the original order
    "residual",           # cycle does not tie to the reported payout
]


class Evidence(BaseModel):
    """What the investigator found. Always a suggestion, never a decision."""

    summary: str
    confidence: int = Field(ge=0, le=100)
    suggested_account: Optional[str] = None
    suggested_side: Optional[Side] = None
    source: Literal["rules", "prior_cycle", "llm"] = "rules"


class ReconException(BaseModel):
    key: str
    platform: Platform
    kind: ExceptionKind
    line: Optional[SettlementLine] = None
    amount: float
    why: str
    evidence: Optional[Evidence] = None
    resolved: bool = False
    resolved_account: Optional[str] = None
    resolved_by: Optional[str] = None


class JournalLine(BaseModel):
    account: str
    side: Side
    amount: float


class JournalEntry(BaseModel):
    platform: Platform
    cycle: str
    lines: list[JournalLine]
    reference: str

    @property
    def total_debit(self) -> float:
        return round(sum(l.amount for l in self.lines if l.side == Side.DEBIT), 2)

    @property
    def total_credit(self) -> float:
        return round(sum(l.amount for l in self.lines if l.side == Side.CREDIT), 2)

    @property
    def balanced(self) -> bool:
        return abs(self.total_debit - self.total_credit) < 0.005


class CycleResult(BaseModel):
    platform: Platform
    cycle: str
    classified_total: float
    reported_payout: float
    residual: float
    exceptions: list[ReconException]
    journal: Optional[JournalEntry] = None

    @property
    def ties_out(self) -> bool:
        return abs(self.residual) < 0.005 and not [e for e in self.exceptions if not e.resolved]


class AuditRecord(BaseModel):
    at: datetime
    kind: Literal["source", "classify", "exception", "decision", "post", "rule"]
    message: str
    actor: str = "Fynn"
