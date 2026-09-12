"""Ledger adapters.

The core engine is decoupled from the destination ledger. Adding QuickBooks,
Kingdee or SQL Account means writing an adapter, not touching reconciliation
logic.

Every adapter takes the firm's account map, because a journal line carries an
account *name* and no ledger accepts one — see models/account_map.py. An
adapter that cannot resolve a name refuses the whole entry rather than posting a
partial or guessed one.

Note on scope: Fynn posts the entry. It does not match against the bank. Once
the entry is in Xero, Xero's own bank feed auto-matches it to the real deposit.
Building bank access here would duplicate infrastructure the ledger already has.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

from models.account_map import AccountMap
from models.transaction import JournalEntry


class LedgerAdapter(ABC):
    name: str

    def __init__(self, accounts: Optional[AccountMap] = None) -> None:
        self.accounts = accounts

    @abstractmethod
    def post(self, entry: JournalEntry) -> dict:
        ...

    def _check(self, entry: JournalEntry) -> None:
        if not entry.balanced:
            raise ValueError(
                f"Refusing to post an unbalanced entry: "
                f"debits {entry.total_debit} vs credits {entry.total_credit}"
            )

    def _resolve(self, entry: JournalEntry) -> dict[str, str]:
        """Account name → the destination ledger's code, or refuse.

        Posting a journal to the wrong account is worse than not posting it, so
        an unmapped account stops the entry and names itself in the error.
        """
        if self.accounts is None:
            raise RuntimeError(f"No chart of accounts mapping loaded for {self.name}.")
        missing = self.accounts.missing({l.account for l in entry.lines})
        if missing:
            raise RuntimeError(
                f"These accounts are not mapped to {self.name} yet: "
                + ", ".join(missing)
                + ". Map them under Accounts before posting."
            )
        return {
            l.account: self.accounts.get(l.account).code for l in entry.lines
        }


class DryRunAdapter(LedgerAdapter):
    """Default. Returns the payload that would be sent, posts nothing.

    Unlike the live adapters this does not refuse an unmapped account — the
    point of a dry run is to see what is still missing, so the gaps are listed
    alongside the payload instead.
    """

    name = "dry-run"

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        unmapped: list[str] = []
        codes: dict[str, str] = {}
        if self.accounts is not None:
            unmapped = self.accounts.missing({l.account for l in entry.lines})
            codes = {
                l.account: (self.accounts.get(l.account).code
                            if self.accounts.get(l.account) else "")
                for l in entry.lines
            }

        payload = entry.model_dump(mode="json")
        for line, rendered in zip(entry.lines, payload["lines"]):
            rendered["ledger_code"] = codes.get(line.account, "")
        return {
            "status": "not_posted",
            "adapter": self.name,
            "reference": entry.reference,
            "unmapped_accounts": unmapped,
            "payload": payload,
        }


class XeroAdapter(LedgerAdapter):
    """Xero manual journal posting.

    Requires OAuth 2.0 with the accounting.transactions scope and a refresh-token
    flow (access tokens expire after 30 minutes). Not wired up: the credentials
    and consent flow have to exist first.
    """

    name = "xero"
    ENDPOINT = "https://api.xero.com/api.xro/2.0/ManualJournals"

    def __init__(
        self,
        accounts: Optional[AccountMap] = None,
        access_token: str | None = None,
        tenant_id: str | None = None,
    ):
        super().__init__(accounts)
        self.access_token = access_token or os.getenv("XERO_ACCESS_TOKEN")
        self.tenant_id = tenant_id or os.getenv("XERO_TENANT_ID")

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        codes = self._resolve(entry)
        if not self.access_token:
            raise RuntimeError(
                "XERO_ACCESS_TOKEN is required. Complete the OAuth flow first."
            )
        # Entries are posted as DRAFT. An accountant approves in Xero before the
        # entry is final — no automated output becomes accounting truth unreviewed.
        payload = {
            "ManualJournals": [{
                "Narration": f"Fynn — {entry.platform.value} {entry.cycle}",
                "Status": "DRAFT",
                "JournalLines": [
                    {
                        "AccountCode": codes[l.account],
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


class QuickBooksAdapter(LedgerAdapter):
    """QuickBooks Online journal entry posting.

    Requires OAuth 2.0 with the com.intuit.quickbooks.accounting scope and the
    company's realm id, which the consent flow returns. Not wired up, for the
    same reason as Xero.

    QuickBooks takes the account's *Id* from the chart of accounts in
    AccountRef.value, not its name or number, and each line states its own
    PostingType rather than carrying a signed amount.
    """

    name = "quickbooks"
    ENDPOINT = "https://quickbooks.api.intuit.com/v3/company/{realm_id}/journalentry"

    def __init__(
        self,
        accounts: Optional[AccountMap] = None,
        access_token: str | None = None,
        realm_id: str | None = None,
    ):
        super().__init__(accounts)
        self.access_token = access_token or os.getenv("QBO_ACCESS_TOKEN")
        self.realm_id = realm_id or os.getenv("QBO_REALM_ID")

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        codes = self._resolve(entry)
        if not self.access_token or not self.realm_id:
            raise RuntimeError(
                "QBO_ACCESS_TOKEN and QBO_REALM_ID are required. "
                "Complete the OAuth flow first."
            )
        payload = {
            "DocNumber": entry.reference,
            "PrivateNote": f"Fynn — {entry.platform.value} {entry.cycle}",
            "Line": [
                {
                    "Description": f"{entry.reference} · {l.account}",
                    "Amount": abs(l.amount),
                    "DetailType": "JournalEntryLineDetail",
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit" if l.side.value == "debit" else "Credit",
                        "AccountRef": {"value": codes[l.account], "name": l.account},
                    },
                }
                for l in entry.lines
            ],
        }
        raise NotImplementedError(
            "QuickBooks HTTP call not implemented. Payload prepared: " + str(payload)
        )


ADAPTERS = {
    DryRunAdapter.name: DryRunAdapter,
    XeroAdapter.name: XeroAdapter,
    QuickBooksAdapter.name: QuickBooksAdapter,
}


def get_adapter(
    name: str | None = None, accounts: Optional[AccountMap] = None
) -> LedgerAdapter:
    name = (name or os.getenv("LEDGER_ADAPTER", "dry-run")).lower()
    return ADAPTERS.get(name, DryRunAdapter)(accounts)
