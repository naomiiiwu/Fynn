"""Reconciliation Agent — verifies payout math for a single platform."""

from models.transaction import ReconciliationResult, Transaction
from services.reconciliation import reconcile


class ReconciliationAgent:
    """Wraps the reconciliation service. No Claude call — pure math."""

    def run(
        self,
        transactions: list[Transaction],
        platform: str,
        period: str,
    ) -> ReconciliationResult:
        print(f"  [Reconciliation:{platform}] Reconciling {len(transactions)} transactions...")
        return reconcile(transactions)
