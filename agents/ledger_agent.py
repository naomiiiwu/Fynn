"""Ledger Agent — builds double-entry journal entries for a platform/period."""

from models.ledger import JournalEntry
from models.transaction import ReconciliationResult
from services.ledger import build_journal_entries


class LedgerAgent:
    """Wraps the ledger service. No Claude call — pure math."""

    def run(
        self,
        reconciliation: ReconciliationResult,
        cost_totals_myr: dict[str, float],
        platform: str,
        period: str,
    ) -> list[JournalEntry]:
        print(f"  [Ledger:{platform}] Building journal entries for {period}...")
        entries = build_journal_entries(reconciliation, cost_totals_myr, platform, period)
        for entry in entries:
            print(f"    {entry.description}: {len(entry.lines)} lines, balanced={entry.is_balanced()}")
        return entries
