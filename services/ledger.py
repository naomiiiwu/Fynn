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

import hashlib
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from models.account_map import AccountMap
from models.transaction import JournalEntry


class LedgerAdapter(ABC):
    name: str

    def __init__(
        self, accounts: Optional[AccountMap] = None, connection=None
    ) -> None:
        self.accounts = accounts
        self.connection = connection

    def idempotency_key(self, entry: JournalEntry) -> str:
        """Stable for a given entry, so a retry cannot create a second journal.

        Derived from the firm, the cycle and the entry reference rather than
        from the moment of sending: a network timeout followed by a retry must
        present the same key, or the retry is a new journal in a client's books.

        The corollary is that deliberately re-posting a corrected entry for the
        same cycle is also deduplicated. That is the safer way round — a
        duplicate is silent and a rejection is not.
        """
        firm = getattr(self.accounts, "firm_id", "") or ""
        raw = f"{firm}|{self.name}|{entry.cycle}|{entry.reference}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

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
        connection=None,
        access_token: str | None = None,
        tenant_id: str | None = None,
    ):
        super().__init__(accounts, connection)
        self._token = access_token or os.getenv("XERO_ACCESS_TOKEN")
        self._tenant = tenant_id or os.getenv("XERO_TENANT_ID")

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        codes = self._resolve(entry)
        token = self._token or (self.connection.access_token() if self.connection else None)
        tenant = self._tenant or (self.connection.org_id if self.connection else None)
        if not token:
            raise RuntimeError(
                "Xero is not connected. Connect it in Settings first."
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
        response = httpx.post(
            self.ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                # Not required for a custom connection, which covers one
                # organisation, but required for a standard app.
                **({"Xero-tenant-id": tenant} if tenant else {}),
                "Accept": "application/json",
                "Idempotency-Key": self.idempotency_key(entry),
            },
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Xero refused {entry.reference} ({response.status_code}): "
                f"{response.text[:300]}"
            )
        body = response.json()
        posted = (body.get("ManualJournals") or [{}])[0]
        return {
            "status": "posted",
            "adapter": self.name,
            "reference": entry.reference,
            "ledger_id": posted.get("ManualJournalID", ""),
            "ledger_status": posted.get("Status", "DRAFT"),
        }


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
        connection=None,
        access_token: str | None = None,
        realm_id: str | None = None,
    ):
        super().__init__(accounts, connection)
        self._token = access_token or os.getenv("QBO_ACCESS_TOKEN")
        self._realm = realm_id or os.getenv("QBO_REALM_ID")

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        codes = self._resolve(entry)
        token = self._token or (self.connection.access_token() if self.connection else None)
        realm = self._realm or (self.connection.org_id if self.connection else None)
        if not token or not realm:
            raise RuntimeError(
                "QuickBooks is not connected. Connect it in Settings first."
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
        # RequestId is Intuit's idempotency control: the same value replays the
        # original result rather than creating a second entry.
        url = self.ENDPOINT.format(realm_id=realm)
        response = httpx.post(
            url,
            json=payload,
            params={"minorversion": "75", "requestid": self.idempotency_key(entry)},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=45,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"QuickBooks refused {entry.reference} ({response.status_code}): "
                f"{response.text[:300]}"
            )
        body = response.json()
        posted = body.get("JournalEntry", {})
        return {
            "status": "posted",
            "adapter": self.name,
            "reference": entry.reference,
            "ledger_id": posted.get("Id", ""),
            "ledger_status": "posted",
        }


ADAPTERS = {
    DryRunAdapter.name: DryRunAdapter,
    XeroAdapter.name: XeroAdapter,
    QuickBooksAdapter.name: QuickBooksAdapter,
}


def get_adapter(
    name: str | None = None,
    accounts: Optional[AccountMap] = None,
    connection=None,
) -> LedgerAdapter:
    name = (name or os.getenv("LEDGER_ADAPTER", "dry-run")).lower()
    return ADAPTERS.get(name, DryRunAdapter)(accounts, connection)


# ── Reading the destination's chart of accounts ───────────────────────────────

def fetch_chart(ledger: str, connection) -> list[dict]:
    """The firm's chart of accounts, as the ledger holds it.

    Typing account codes by hand is the worst part of setup and the easiest
    place to put a journal in the wrong account. Once a ledger is connected its
    own chart can be read, so the mapping becomes a choice from a list.

    Returns [{code, name, type}] sorted by code, where `code` is whatever that
    ledger expects in a journal line — a Xero AccountCode, a QuickBooks Id.
    """
    if connection is None:
        raise RuntimeError(f"{ledger} is not connected.")
    token = connection.access_token()

    if ledger == "xero":
        response = httpx.get(
            "https://api.xero.com/api.xro/2.0/Accounts",
            headers={
                "Authorization": f"Bearer {token}",
                **({"Xero-tenant-id": connection.org_id} if connection.org_id else {}),
                "Accept": "application/json",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Xero refused the chart request "
                               f"({response.status_code}): {response.text[:200]}")
        out = []
        for account in response.json().get("Accounts", []):
            # An account with no code cannot be referenced in a journal line, so
            # it is not offered as a destination.
            if not account.get("Code"):
                continue
            out.append({
                "code": account["Code"],
                "name": account.get("Name", ""),
                "type": account.get("Type", ""),
            })
        return sorted(out, key=lambda a: a["code"])

    if ledger == "quickbooks":
        response = httpx.get(
            f"https://quickbooks.api.intuit.com/v3/company/{connection.org_id}/query",
            params={"query": "select Id, Name, AccountType, Active from Account maxresults 1000",
                    "minorversion": "75"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"QuickBooks refused the chart request "
                               f"({response.status_code}): {response.text[:200]}")
        accounts = response.json().get("QueryResponse", {}).get("Account", [])
        return sorted(
            [
                {"code": a["Id"], "name": a.get("Name", ""), "type": a.get("AccountType", "")}
                for a in accounts if a.get("Active", True)
            ],
            key=lambda a: a["name"].lower(),
        )

    raise RuntimeError(f"No chart of accounts available for {ledger}.")
