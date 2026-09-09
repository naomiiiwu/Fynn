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


class RuleStore:
    """Per-firm rules. Persisted per firm_id (the firm's WhatsApp number).

    The switching cost lives here: after a year a firm has accumulated hundreds
    of decisions, applied consistently across every client they manage.
    """

    def __init__(
        self, rules: Optional[list[Rule]] = None, firm_id: Optional[str] = None
    ) -> None:
        self._rules: list[Rule] = list(rules) if rules is not None else [
            r.model_copy() for r in DEFAULT_RULES
        ]
        self.firm_id = firm_id

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
        """Load a firm's rules from storage, falling back to the baseline set."""
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
