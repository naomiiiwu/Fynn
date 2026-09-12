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


# Words that identify what an account is for, beyond a fuzzy name match.
# "Marketing Expense" and "Advertising" share no characters but are the same
# idea; "Sales Revenue" and "Sales Returns" share almost all of them and are
# opposites. Keywords fix both cases where plain similarity cannot.
_HINTS: dict[str, tuple[str, ...]] = {
    "sales revenue":               ("sales", "revenue", "income", "turnover"),
    "sales returns & allowances":  ("return", "refund", "allowance", "credit note"),
    "commission expense":          ("commission",),
    "payment processing fees":     ("payment", "merchant", "bank fee", "transaction fee",
                                    "processing"),
    "platform service fees":       ("service fee", "platform", "subscription"),
    "marketing expense":           ("marketing", "advertis", "promotion", "campaign"),
    "shipping expense":            ("shipping", "freight", "courier", "postage", "delivery"),
    "shipping income":             ("shipping", "freight", "delivery", "courier"),
    "warehouse & storage":         ("storage", "warehouse", "fulfil", "fulfill"),
    "gst input tax":               ("gst", "vat", "input tax", "sst", "tax"),
    "withholding tax receivable":  ("withholding", "wht"),
    "other income":                ("other income", "sundry", "miscellaneous"),
    "other expense":               ("other expense", "sundry", "general expense"),
    "clearing account":            ("clearing", "suspense", "holding", "undeposited"),
}

# Which side of the ledger an account belongs on, so a revenue line is never
# suggested an expense account however similar the names look.
_CONTRA = ("return", "refund", "allowance", "credit note", "reversal", "discount")
_INCOME = ("revenue", "income")
_EXPENSE = ("expense", "fee", "cost", "returns", "tax")


def _hints_for(account: str) -> tuple[str, ...]:
    key = account.strip().lower()
    if key in _HINTS:
        return _HINTS[key]
    for suffix, words in _HINTS.items():
        if key.endswith(suffix):          # "Shopee Clearing Account"
            return words
    return ()


def suggest(account: str, chart: list[dict]) -> Optional[str]:
    """The code in `chart` most likely to be what `account` means, or None.

    A suggestion, never a decision: it arrives pre-selected so the common case
    is one glance rather than one lookup, and the accountant changes it or
    leaves it. Nothing is saved until they say so.
    """
    import difflib

    if not chart:
        return None
    wanted = account.strip().lower()
    hints = _hints_for(account)
    best, best_score = None, 0.0

    for entry in chart:
        name = (entry.get("name") or "").strip().lower()
        if not name:
            continue
        score = difflib.SequenceMatcher(None, wanted, name).ratio()
        # A shared keyword is far stronger evidence than character overlap.
        if any(h in name for h in hints):
            score += 0.55
        # An exact word in common helps too, but only a real word.
        shared = {w for w in wanted.split() if len(w) > 3} & set(name.split())
        score += 0.1 * len(shared)
        # A contra account reads almost identically to the account it offsets —
        # "Sales Revenue" against "Sales Returns" scores 0.77 on characters
        # alone — and putting revenue in a returns account is the worst miss
        # available. Only offer one when the line is itself a contra.
        if any(c in name for c in _CONTRA) and not any(c in wanted for c in _CONTRA):
            score -= 0.8
        # A catch-all account is rarely what a specific line means. Without
        # this, "Sales Revenue" matches "Other Revenue" more closely than it
        # matches "Sales", and revenue quietly lands in the wrong account.
        if any(w in name for w in ("other", "sundry", "miscellaneous")) and \
                not any(w in wanted for w in ("other", "sundry", "miscellaneous")):
            score -= 0.45
        # Do not offer an expense account for a revenue line, or the reverse.
        kind = (entry.get("type") or "").lower()
        if any(w in wanted for w in _INCOME) and any(w in kind for w in ("expense", "cost")):
            score -= 0.6
        if any(w in wanted for w in _EXPENSE) and "income" in kind and "returns" not in wanted:
            score -= 0.4
        # Only offer a match that rests on something: a shared keyword, or
        # names that genuinely look alike. Character overlap alone matched
        # "Platform Service Fees" to "Bank Fees", which is a guess wearing a
        # suggestion's clothes.
        grounded = any(h in name for h in hints) or \
            difflib.SequenceMatcher(None, wanted, name).ratio() >= 0.62
        if grounded and score > best_score:
            best, best_score = entry.get("code"), score

    # Below this the guess is noise, and a blank field is more honest than a
    # confident wrong answer an accountant might not check.
    return best if best_score >= 0.62 else None
