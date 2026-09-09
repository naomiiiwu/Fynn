"""Ledger adapters.

The core engine is decoupled from the destination ledger. Adding QuickBooks,
Kingdee or SQL Account means writing an adapter, not touching reconciliation
logic.

Note on scope: Fynn posts the entry. It does not match against the bank. Once
the entry is in Xero, Xero's own bank feed auto-matches it to the real deposit.
Building bank access here would duplicate infrastructure the ledger already has.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from models.transaction import JournalEntry


class LedgerAdapter(ABC):
    name: str

    @abstractmethod
    def post(self, entry: JournalEntry) -> dict:
        ...


class DryRunAdapter(LedgerAdapter):
    """Default. Returns the payload that would be sent, posts nothing."""

    name = "dry-run"

    def post(self, entry: JournalEntry) -> dict:
        if not entry.balanced:
            raise ValueError(
                f"Refusing to post an unbalanced entry: "
                f"debits {entry.total_debit} vs credits {entry.total_credit}"
            )
        return {
            "status": "not_posted",
            "adapter": self.name,
            "reference": entry.reference,
            "payload": entry.model_dump(mode="json"),
        }


class XeroAdapter(LedgerAdapter):
    """Xero manual journal posting.

    Requires OAuth 2.0 with the accounting.transactions scope, a tenant id, and
    a refresh-token flow (access tokens expire after 30 minutes). Not wired up:
    the credentials and consent flow have to exist first.
    """

    name = "xero"
    ENDPOINT = "https://api.xero.com/api.xro/2.0/ManualJournals"

    def __init__(self, access_token: str | None = None, tenant_id: str | None = None):
        self.access_token = access_token or os.getenv("XERO_ACCESS_TOKEN")
        self.tenant_id = tenant_id or os.getenv("XERO_TENANT_ID")

    def post(self, entry: JournalEntry) -> dict:
        if not entry.balanced:
            raise ValueError("Refusing to post an unbalanced entry")
        if not self.access_token or not self.tenant_id:
            raise RuntimeError(
                "XERO_ACCESS_TOKEN and XERO_TENANT_ID are required. "
                "Complete the OAuth flow first."
            )
        # Entries are posted as DRAFT. An accountant approves in Xero before the
        # entry is final — no automated output becomes accounting truth unreviewed.
        payload = {
            "ManualJournals": [{
                "Narration": f"Fynn — {entry.platform.value} {entry.cycle}",
                "Status": "DRAFT",
                "JournalLines": [
                    {
                        "AccountCode": l.account,
                        "LineAmount": l.amount if l.side.value == "debit" else -l.amount,
                        "Description": f"{entry.reference} · {l.account}",
                    }
                    for l in entry.lines
                ],
            }]
        }
        raise NotImplementedError(
            "Xero HTTP call not implemented. Payload prepared: " + str(payload)
        )


def get_adapter(name: str | None = None) -> LedgerAdapter:
    name = (name or os.getenv("LEDGER_ADAPTER", "dry-run")).lower()
    if name == "xero":
        return XeroAdapter()
    return DryRunAdapter()
