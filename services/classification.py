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

# Classifications that hold fees belonging to different accounts, so one
# decision must never be widened across them. Derived from Lazada's published
# taxonomy rather than asserted: a classification whose terms land in more than
# one of BigSeller's profit buckets cannot carry a single treatment —
# "Orders-Lazada Fees" spans six of them, "Orders-Sales" three.
#
# Bucket spread alone is not sufficient, because BigSeller sometimes files
# plainly different fees under one bucket, so a short named list is unioned in.
# See data/lazada_taxonomy.ACCOUNT_HETEROGENEOUS.
def _mixed_categories() -> set[str]:
    from data.lazada_taxonomy import ACCOUNT_HETEROGENEOUS, classifications

    spread = {name for name, buckets in classifications().items() if len(buckets) > 1}
    return {name.strip().lower() for name in spread | ACCOUNT_HETEROGENEOUS}


MIXED_CATEGORIES = _mixed_categories()


def is_mixed_category(category: Optional[str]) -> bool:
    return (category or "").strip().lower() in MIXED_CATEGORIES


# Labels that must never become a rule, however often they are approved.
# Lazada ships an explicit catch-all for fees outside its own taxonomy: what
# arrives under it differs every cycle, so a rule would silently post next
# month's unknown charge to last month's account.
NEVER_RULE = ("does not belong to the terms above", "other fee", "miscellaneous")


def is_never_rule(label: str) -> bool:
    lowered = (label or "").strip().lower()
    return any(hint in lowered for hint in NEVER_RULE)


class RuleStore:
    """Per-firm rules, persisted per firm_id.

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
        """The most specific rule covering this line.

        Order of precedence, most specific first: a rule naming this exact fee
        on this platform, then one naming the fee on any platform, then one
        covering the platform's whole classification. A firm that has decided
        how to treat "Campaign Fee" must not have that overridden by its more
        general decision about Lazada marketing fees.
        """
        def ranked(candidates: list[Rule]) -> list[Rule]:
            return [r for r in candidates if r.matches(line)]

        by_label = [r for r in self._rules if r.category is None]
        by_category = [r for r in self._rules if r.category is not None]

        for pool in (
            [r for r in by_label if r.platform is not None],
            [r for r in by_label if r.platform is None],
            [r for r in by_category if r.platform is not None],
            [r for r in by_category if r.platform is None],
        ):
            hits = ranked(pool)
            if hits:
                return hits[0]
        return None

    def add(
        self,
        line: SettlementLine,
        account: str,
        side: Side,
        decided_by: str,
        platform_specific: bool = True,
        scope: str = "label",
    ) -> Rule:
        """Save a decision as a rule.

        scope="category" writes it against the platform's own classification
        instead of the single fee name, so every fee filed under that heading —
        including ones that have never appeared before — inherits the treatment.
        """
        # A classification that holds fees belonging to different accounts
        # cannot carry one decision, so the rule narrows back to the fee name.
        as_category = (
            scope == "category"
            and bool(line.category)
            and not is_mixed_category(line.category)
        )
        rule = Rule(
            platform=line.platform if platform_specific else None,
            label="" if as_category else line.label,
            category=line.category if as_category else None,
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
