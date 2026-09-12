"""Mapping Fynn's account names onto a firm's own chart of accounts.

Fynn's journals name accounts in plain terms — "Marketing Expense", "Lazada
Clearing Account". No ledger accepts that. Xero wants an AccountCode ("400");
QuickBooks wants the account's Id ("34"). Both come from the firm's own chart of
accounts, which nobody else can supply: two firms will have different codes for
the same idea, and some will want marketing split across three accounts.

So the mapping belongs to the firm, like the rules do, and posting is refused
while any account a cycle would touch is unmapped. Sending a journal to the
wrong account is worse than not sending it.

Keyed per ledger as well as per firm, because a Xero code and a QuickBooks id
are not interchangeable — a firm that switches ledgers maps again.
"""

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class LedgerAccount:
    """One account as the destination ledger knows it."""

    code: str           # Xero AccountCode, or QuickBooks account Id
    name: str = ""      # what the ledger calls it — shown back for confirmation

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name}


class AccountMap:
    """A firm's mapping for one ledger. Empty until they fill it in."""

    def __init__(
        self,
        firm_id: str,
        ledger: str,
        mapping: Optional[dict[str, LedgerAccount]] = None,
    ) -> None:
        self.firm_id = firm_id
        self.ledger = ledger
        self._mapping: dict[str, LedgerAccount] = dict(mapping or {})

    @staticmethod
    def _key(account: str) -> str:
        return (account or "").strip().lower()

    def get(self, account: str) -> Optional[LedgerAccount]:
        return self._mapping.get(self._key(account))

    def set(self, account: str, code: str, name: str = "") -> Optional[LedgerAccount]:
        """Map one account, or clear it when the code is blank."""
        key = self._key(account)
        code = (code or "").strip()
        if not code:
            self._mapping.pop(key, None)
            self._persist(account, None)
            return None
        entry = LedgerAccount(code=code, name=(name or "").strip())
        self._mapping[key] = entry
        self._persist(account, entry)
        return entry

    def missing(self, accounts: Iterable[str]) -> list[str]:
        """Which of these accounts have no destination yet."""
        return sorted({a for a in accounts if self.get(a) is None})

    def as_dict(self, accounts: Iterable[str]) -> dict[str, dict]:
        """The mapping for a given set of accounts, including the blanks."""
        out: dict[str, dict] = {}
        for account in accounts:
            entry = self.get(account)
            out[account] = entry.to_dict() if entry else {"code": "", "name": ""}
        return out

    def _persist(self, account: str, entry: Optional[LedgerAccount]) -> None:
        """Write one mapping through to storage; never lose it for this session."""
        if not self.firm_id:
            return
        try:
            from services.database import delete_account_mapping, save_account_mapping
            if entry is None:
                delete_account_mapping(self.firm_id, self.ledger, account)
            else:
                save_account_mapping(self.firm_id, self.ledger, account, entry)
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Accounts] Could not persist mapping for {account}: {exc}")

    @classmethod
    def for_firm(cls, firm_id: str, ledger: str) -> "AccountMap":
        try:
            from services.database import load_account_mappings
            rows = load_account_mappings(firm_id, ledger)
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Accounts] Could not load mappings: {exc}")
            rows = []

        mapping: dict[str, LedgerAccount] = {}
        for row in rows:
            account = row.get("fynn_account") or ""
            code = row.get("code") or ""
            if account and code:
                mapping[cls._key(account)] = LedgerAccount(
                    code=code, name=row.get("name") or ""
                )
        return cls(firm_id, ledger, mapping)
