"""Deterministic classification.

Design note: this layer never calls an LLM. Classifying a known fee label is a
lookup, not a judgement — it has one correct answer, and financial data needs
the result to be reproducible. The LLM is confined to agents/investigator.py,
which only handles what this layer could not resolve.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from models.transaction import Platform, Rule, SettlementLine, Side

# Baseline rules every firm starts with. Anything outside this becomes an
# exception on first sight, and the firm's decision is saved as a new rule.
DEFAULT_RULES: list[Rule] = [
    Rule(label="Sale", account="Sales Revenue", side=Side.CREDIT),
    Rule(label="Commission", account="Commission Expense", side=Side.DEBIT),
    Rule(label="Payment fee", account="Payment Processing Fees", side=Side.DEBIT),
    Rule(label="Voucher subsidy", account="Marketing Expense", side=Side.DEBIT),
    Rule(label="Refund", account="Sales Returns & Allowances", side=Side.DEBIT),
    Rule(label="Shipping fee", account="Shipping Expense", side=Side.DEBIT),
]

STARTER_ACTOR = "Fynn starter pack"

# Platform mechanics: fee names where the account is a lookup, not a judgement.
# A firm may rename the account, but none of these has a second defensible
# treatment — a commission is an expense, an item price is revenue, a reversal
# of an item price is a sales return.
#
# What is deliberately NOT here is anything a firm could reasonably book two
# ways, because pre-deciding those would be Fynn making the firm's accounting
# policy for it:
#
#   seller-funded vouchers, discounts, coins, campaign and AMS fees
#       marketing expense, or a reduction of revenue under IFRS 15's
#       "consideration payable to a customer". Firms genuinely split on this.
#   service and withholding taxes
#       recoverable input tax or an expense, depending on registration and
#       jurisdiction.
#   claims and compensation
#       other income, or an offset against the loss being compensated.
#   seller balance adjustments
#       the clearing account, or income and expense.
#
# Sides follow the natural sign of each fee, so that one rule per label keeps
# the summarised entry balanced.
STARTER_RULES: list[Rule] = [
    # ── Shopee ──
    Rule(platform=Platform.SHOPEE, label="Product Price",
         account="Sales Revenue", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Refund Amount",
         account="Sales Returns & Allowances", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Commission Fee",
         account="Commission Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Transaction Fee",
         account="Payment Processing Fees", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Service Fee",
         account="Platform Service Fees", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Actual Shipping Fee",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Shipping Fee Borne by Seller",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Reverse Shipping Fee",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Overseas Return Service Fee",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Delivery Failure Fee",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Shipping Fee Paid by Buyer",
         account="Shipping Income", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.SHOPEE, label="Shipping Fee Rebate From Shopee",
         account="Shipping Income", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    # ── Lazada ──
    Rule(platform=Platform.LAZADA, label="Item Price Credit",
         account="Sales Revenue", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Reversal Item Price",
         account="Sales Returns & Allowances", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Reversal Commission",
         account="Commission Expense", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Commission fee - correction for undercharge",
         account="Commission Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Commission fee refund - correction for overcharge",
         account="Commission Expense", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Payment fee refund - correction for overcharge",
         account="Payment Processing Fees", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Shipping Fee Paid by Seller",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Return shipping fees",
         account="Shipping Expense", side=Side.DEBIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Shipping Fee (Paid By Customer)",
         account="Shipping Income", side=Side.CREDIT, decided_by=STARTER_ACTOR),
    Rule(platform=Platform.LAZADA, label="Storage Fee",
         account="Warehouse & Storage", side=Side.DEBIT, decided_by=STARTER_ACTOR),
]

# Labels that must never become a rule, however often they are approved.
# Lazada ships an explicit catch-all for fees outside its own taxonomy: what
# arrives under it differs every cycle, so a rule would silently post next
# month's unknown charge to last month's account.
NEVER_RULE = ("does not belong to the terms above", "other fee", "miscellaneous")


def is_never_rule(label: str) -> bool:
    lowered = (label or "").strip().lower()
    return any(hint in lowered for hint in NEVER_RULE)


class RuleStore:
    """Per-firm rules. Persisted per firm_id (the firm's WhatsApp number).

    The switching cost lives here: after a year a firm has accumulated hundreds
    of decisions, applied consistently across every client they manage.
    """

    def __init__(
        self,
        rules: Optional[list[Rule]] = None,
        firm_id: Optional[str] = None,
        starter_pack: bool = True,
    ) -> None:
        self._rules: list[Rule] = list(rules) if rules is not None else [
            r.model_copy() for r in DEFAULT_RULES
        ]
        if rules is None and starter_pack:
            self._rules.extend(r.model_copy() for r in STARTER_RULES)
        self.firm_id = firm_id

    @property
    def starter_rules(self) -> list[Rule]:
        """Rules Fynn supplied rather than the firm deciding them."""
        return [r for r in self._rules if r.decided_by == STARTER_ACTOR]

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def find(self, line: SettlementLine) -> Optional[Rule]:
        # Platform-specific rules win over global ones.
        specific = [r for r in self._rules if r.platform is not None and r.matches(line)]
        if specific:
            return specific[0]
        generic = [r for r in self._rules if r.platform is None and r.matches(line)]
        return generic[0] if generic else None

    def add(
        self,
        line: SettlementLine,
        account: str,
        side: Side,
        decided_by: str,
        platform_specific: bool = True,
    ) -> Rule:
        rule = Rule(
            platform=line.platform if platform_specific else None,
            label=line.label,
            account=account,
            side=side,
            decided_by=decided_by,
            decided_at=datetime.now(timezone.utc),
        )
        self._rules.append(rule)
        self._persist(rule)
        return rule

    def _persist(self, rule: Rule) -> None:
        """Write one learned rule through to storage.

        A storage failure must never lose the accountant's decision for the
        current cycle — the rule stays in memory either way.
        """
        if not self.firm_id:
            return
        try:
            from services.database import save_rule
            save_rule(self.firm_id, rule)
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Rules] Could not persist rule for {self.firm_id}: {exc}")

    def export(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self._rules]

    @classmethod
    def load(cls, data: list[dict], firm_id: Optional[str] = None) -> "RuleStore":
        return cls([Rule(**d) for d in data], firm_id=firm_id)

    @classmethod
    def for_firm(cls, firm_id: str) -> "RuleStore":
        """Load a firm's rules from storage, on top of the baseline set."""
        try:
            from services.database import load_rules
            stored = load_rules(firm_id)
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Rules] Could not load rules for {firm_id}: {exc}")
            stored = []

        store = cls(firm_id=firm_id)
        for data in stored:
            try:
                store._rules.append(Rule(**data))
            except Exception:
                continue
        return store


def classify(
    lines: list[SettlementLine], store: RuleStore
) -> tuple[list[tuple[SettlementLine, Rule]], list[SettlementLine]]:
    """Split lines into (classified, unclassified)."""
    classified: list[tuple[SettlementLine, Rule]] = []
    unclassified: list[SettlementLine] = []
    for line in lines:
        rule = store.find(line)
        if rule is None:
            unclassified.append(line)
        else:
            classified.append((line, rule))
    return classified, unclassified
