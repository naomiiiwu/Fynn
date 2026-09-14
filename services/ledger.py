"""Ledger adapters.

The core engine is decoupled from the destination ledger. Adding QuickBooks,
Kingdee or SQL Account means writing an adapter, not touching reconciliation
logic.

Every adapter takes the firm's account map, because a journal line carries an
account *name* and no ledger accepts one — see models/account_map.py. An
adapter that cannot resolve a name refuses the whole entry rather than posting a
partial or guessed one.

Fynn posts; it does not touch the bank. The ledger already has the bank feed,
and duplicating that would be building infrastructure twice.

But *what* is posted decides whether the ledger can reconcile it, and the two
ledgers differ — A2X, which has done this at scale for years, posts a different
shape to each, and the reason is mechanical:

  Xero        A draft ACCREC invoice, or an ACCPAY bill when the payout is
              negative. Xero's bank reconciliation offers a match against
              invoices and bills; a manual journal is not something a statement
              line can be matched to, so a journal leaves the accountant coding
              the deposit by hand every payout.
              <https://support.a2xaccounting.com/> — "Posting Your A2X
              Summaries": the entry "appears as a Draft Invoice, or a Draft Bill
              if it's negative... Xero will suggest a match to the approved
              invoice."

  QuickBooks  A journal entry, which QuickBooks *does* offer for matching
              against a bank deposit — same guide: "simply click Match."

So the invoice is not a stylistic choice. It is what makes the deposit
reconcilable in Xero without manual work.
"""
from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from models.account_map import AccountMap
from models.transaction import JournalEntry


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _payout_date(payout: str) -> str:
    """The day a settlement period closed, from the platform's own wording.

    An invoice dated when Fynn happened to run is harder to reconcile and lands
    in the wrong period at a year end. The end of the stated range is the
    settlement date; anything unparseable falls back to today rather than
    guessing.
    """
    import re as _re
    from datetime import datetime
    if not payout:
        return ""
    parts = _re.split(r"\s+(?:-|–|—|to)\s+", payout.strip())
    for text in reversed(parts):
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(text.strip(), fmt).date().isoformat()
            except ValueError:
                continue
    return ""


def split_clearing(entry: JournalEntry):
    """The clearing line, and everything else.

    The clearing line carries the net — what the platform deposits — and the
    rest explain how gross sales became that figure. Adapters that post a
    document rather than a journal need that split: one is the total, the others
    are the lines.
    """
    clearing = next(
        (l for l in entry.lines if l.account.lower().endswith("clearing account")),
        None,
    )
    rest = [l for l in entry.lines if l is not clearing]
    return clearing, rest


class LedgerAdapter(ABC):
    name: str
    # Whether this ledger wants one document per bank deposit. True where the
    # ledger reconciles a statement line against a document, because a document
    # covering several deposits matches none of them.
    per_payout = False

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

    def _check_scope(self) -> None:
        """Refuse a post the connection was never authorised to make.

        Without this the attempt reaches the provider and comes back a 403 with
        the provider's own wording, after the entry has been built and the firm
        has been told it is posting. Better to say so before anything is sent.
        """
        if self.connection is None or self.connection.can_post:
            return
        raise RuntimeError(
            f"Fynn is connected to {self.name} for reading only — the "
            f"authorisation does not include permission to write journals. "
            f"Enable the posting scope on the app, then reconnect under Settings."
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

    def __init__(self, accounts=None, connection=None, preview_for: str = "") -> None:
        super().__init__(accounts, connection)
        # A dry run of Xero must split the way a real Xero post would, or it is
        # a dry run of something else.
        self.per_payout = preview_for == "xero"
        # Which live adapter's document to render alongside. Only Xero builds a
        # document distinct from the journal, so only Xero has one to preview.
        self.preview_for = preview_for if preview_for == "xero" else ""

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
            mapped = self.accounts.get(line.account) if self.accounts else None
            rendered["tax"] = getattr(mapped, "tax", "") or ""

        # What the live adapter would actually send. A dry run that shows a
        # different shape to the real post is a dry run of nothing — and the
        # document, not the journal, is what an accountant needs to check.
        document = None
        if self.preview_for and not unmapped:
            try:
                document = get_adapter(self.preview_for, self.accounts).build_payload(entry)
            except Exception as exc:      # a preview must never break the dry run
                document = {"error": str(exc)}

        return {
            "status": "not_posted",
            "adapter": self.name,
            "reference": entry.reference,
            "unmapped_accounts": unmapped,
            "payload": payload,
            "would_send_to": self.preview_for or "",
            "document": document,
        }


class XeroAdapter(LedgerAdapter):
    """Xero posting, as a draft invoice or bill.

    Not a manual journal, which is what this used to send. Xero's bank
    reconciliation offers a match against invoices and bills; a manual journal
    is not something a statement line can be matched to. Posting a journal
    therefore left the accountant coding every payout against the clearing
    account by hand — the exact work Fynn exists to remove. See the module
    docstring for the source.

    The shape follows from the journal rather than replacing it:

      the clearing line   is the document total — what the platform actually
                          deposits, and what the bank statement will show.
      every other line    becomes an invoice line explaining how gross sales
                          became that figure: revenue positive, fees negative.

    A payout is normally money in, so the document is an ACCREC invoice. A cycle
    where the fees exceeded the sales is money out, and Xero has a different
    document for that: an ACCPAY bill, with the signs flipped so the total is
    positive. Both post as DRAFT — an accountant approves in Xero before
    anything is final.
    """

    name = "xero"
    per_payout = True
    ENDPOINT = "https://api.xero.com/api.xro/2.0/Invoices"

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

    def build_payload(self, entry: JournalEntry) -> dict:
        """The document Xero would receive. Separated so a dry run can show it."""
        clearing, rest = split_clearing(entry)
        if clearing is None:
            raise RuntimeError(
                f"{entry.reference} has no clearing line, so there is no payout "
                "total to invoice. This is a bug in the journal builder."
            )

        # The clearing line is a debit for money in. Its signed value is the
        # payout, and the document type follows the direction.
        net = clearing.amount if clearing.side.value == "debit" else -clearing.amount
        money_in = net >= 0
        flip = 1 if money_in else -1

        lines = []
        for line in rest:
            # A credit is money in and belongs on the invoice as a positive
            # amount; a debit is money out and reduces the total.
            amount = line.amount if line.side.value == "credit" else -line.amount
            item = {
                "Description": f"{entry.reference} · {line.account}",
                "Quantity": 1,
                "UnitAmount": round(amount * flip, 2),
                "AccountCode": self._code(line.account),
            }
            tax = self._tax(line.account)
            if tax:
                item["TaxType"] = tax
            lines.append(item)

        return {
            "Invoices": [{
                "Type": "ACCREC" if money_in else "ACCPAY",
                # Xero creates the contact if this name is new, so a firm does
                # not have to set one up before the first post.
                "Contact": {"Name": entry.platform.value},
                "Date": _payout_date(entry.payout) or _today(),
                "LineAmountTypes": "Exclusive",
                "InvoiceNumber": entry.reference,
                "Reference": f"Fynn — {entry.platform.value} {entry.cycle}",
                "Status": "DRAFT",
                "LineItems": lines,
            }]
        }

    def _code(self, account: str) -> str:
        return self.accounts.get(account).code

    def _tax(self, account: str) -> str:
        entry = self.accounts.get(account) if self.accounts else None
        return getattr(entry, "tax", "") or ""

    def post(self, entry: JournalEntry) -> dict:
        self._check(entry)
        self._check_scope()
        self._resolve(entry)          # refuses before building anything
        token = self._token or (self.connection.access_token() if self.connection else None)
        tenant = self._tenant or (self.connection.org_id if self.connection else None)
        if not token:
            raise RuntimeError(
                "Xero is not connected. Connect it in Settings first."
            )
        response = httpx.post(
            self.ENDPOINT,
            json=self.build_payload(entry),
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
        if response.status_code in (401, 403):
            # Xero answers a scope it did not grant with a bare
            # "AuthorizationUnsuccessful", which reads as a broken login. The
            # token is fine — it was never given permission to write. This is
            # the likeliest reason a connection that can read the chart of
            # accounts cannot post to it.
            raise RuntimeError(
                f"Xero would not accept {entry.reference}: the connection is not "
                f"authorised to create invoices. Enable accounting.invoices on "
                f"your Xero app — or accounting.transactions if it is an older "
                f"app that still uses the broad scopes — then disconnect and "
                f"reconnect Xero in Settings. (Xero said: {response.status_code} "
                f"{response.text[:120]})"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Xero refused {entry.reference} ({response.status_code}): "
                f"{response.text[:300]}"
            )
        body = response.json()
        posted = (body.get("Invoices") or [{}])[0]
        return {
            "status": "posted",
            "adapter": self.name,
            "reference": entry.reference,
            "ledger_id": posted.get("InvoiceID", ""),
            "ledger_status": posted.get("Status", "DRAFT"),
            "document": posted.get("Type", ""),
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

    def _tax(self, account: str) -> str:
        entry = self.accounts.get(account) if self.accounts else None
        return getattr(entry, "tax", "") or ""

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
        self._check_scope()
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
                        # Only when the firm has mapped one. Absent, QuickBooks
                        # applies the account's own default rather than a rate
                        # Fynn invented.
                        **({"TaxCodeRef": {"value": self._tax(l.account)}}
                           if self._tax(l.account) else {}),
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
    preview_for: str = "",
) -> LedgerAdapter:
    name = (name or os.getenv("LEDGER_ADAPTER", "dry-run")).lower()
    cls = ADAPTERS.get(name, DryRunAdapter)
    if cls is DryRunAdapter:
        return cls(accounts, connection, preview_for=preview_for)
    return cls(accounts, connection)


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
