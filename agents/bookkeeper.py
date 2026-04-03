"""
Fynn AI Bookkeeper Agent.

Uses Claude (claude-sonnet-4-6) for:
1. Transaction categorization with few-shot prompting
2. Anomaly detection with human-readable explanations
"""

import json
import os
from typing import List

import anthropic

from models.transaction import Anomaly, Category, ReconciliationResult, Transaction, TransactionType

# Anomaly thresholds
LARGE_REFUND_THRESHOLD_PCT = 20.0   # refund > 20% of original order value
HIGH_FEE_THRESHOLD_PCT = 10.0       # platform fees > 10% of gross revenue
PAYOUT_DISCREPANCY_THRESHOLD_PCT = 2.0

# Few-shot examples for categorization prompt
FEW_SHOT_EXAMPLES = """
Example 1:
Transaction: "Order payment received - ORD-2026-001, amount MYR 85.00"
→ Category: REVENUE, Confidence: 0.99

Example 2:
Transaction: "Shopee commission fee (Week 1: Mar 1-7), amount MYR -57.10"
→ Category: PLATFORM_FEE, Confidence: 0.98

Example 3:
Transaction: "Refund issued - item not received, order ORD-2026-003, amount MYR -45.00"
→ Category: REFUND, Confidence: 0.99
"""


class BookkeeperAgent:
    """AI-powered bookkeeping agent using Claude for categorization and anomaly detection."""

    def __init__(self) -> None:
        """Initialise the Anthropic client."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("  [Bookkeeper] WARNING: ANTHROPIC_API_KEY not set — AI features will be limited.")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        self.model = "claude-sonnet-4-6"

    def _call_claude(self, prompt: str) -> str:
        """
        Send a prompt to Claude and return the text response.

        Args:
            prompt: The full user message to send.

        Returns:
            Claude's text response, or an error string if the call fails.
        """
        if self.client is None:
            return '{"error": "No API key configured"}'
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as exc:
            print(f"  [Bookkeeper] Claude API error: {exc}")
            return '{"error": "API call failed"}'

    def categorize_transaction(self, txn: Transaction) -> Transaction:
        """
        Use Claude to assign an accounting category to a single transaction.

        Uses few-shot prompting to ensure consistent, structured output.

        Args:
            txn: The transaction to categorize.

        Returns:
            The same transaction with category and confidence_score populated.
        """
        # For obvious types, skip the API call and map directly
        direct_map = {
            TransactionType.ORDER: (Category.REVENUE, 1.0),
            TransactionType.REFUND: (Category.REFUND, 1.0),
            TransactionType.PLATFORM_FEE: (Category.PLATFORM_FEE, 1.0),
            TransactionType.SHIPPING: (Category.SHIPPING, 1.0),
            TransactionType.VOUCHER: (Category.VOUCHER, 1.0),
            TransactionType.SETTLEMENT: (Category.OTHER, 1.0),
        }

        if txn.type in direct_map:
            cat, conf = direct_map[txn.type]
            txn.category = cat
            txn.confidence_score = conf
            return txn

        # For ambiguous OTHER types, use Claude
        prompt = f"""You are a bookkeeping AI. Categorize the following e-commerce transaction into exactly one category.

Valid categories: REVENUE, PLATFORM_FEE, REFUND, SHIPPING, VOUCHER, OTHER

{FEW_SHOT_EXAMPLES}

Transaction to categorize:
"{txn.description}, amount MYR {txn.amount_myr:.2f}"

Respond with JSON only, no explanation:
{{"category": "<CATEGORY>", "confidence": <0.0-1.0>}}"""

        raw = self._call_claude(prompt)
        try:
            result = json.loads(raw)
            txn.category = Category(result.get("category", "OTHER"))
            txn.confidence_score = float(result.get("confidence", 0.5))
        except Exception:
            txn.category = Category.OTHER
            txn.confidence_score = 0.5

        return txn

    def categorize_all(self, transactions: List[Transaction]) -> List[Transaction]:
        """
        Categorize all transactions in a list.

        Args:
            transactions: Raw list of transactions.

        Returns:
            Transactions with category and confidence_score populated.
        """
        print(f"\n[Bookkeeper] Categorizing {len(transactions)} transactions...")
        categorized = [self.categorize_transaction(t) for t in transactions]

        # Quick summary
        from collections import Counter
        counts = Counter(t.category.value for t in categorized if t.category)
        for cat, count in sorted(counts.items()):
            print(f"  {cat}: {count} transactions")

        return categorized

    def detect_anomalies(
        self,
        transactions: List[Transaction],
        reconciliation: ReconciliationResult,
    ) -> List[Anomaly]:
        """
        Detect anomalies using rule-based checks and Claude explanations.

        Checks:
        1. Large refund (> 20% of the matched order value)
        2. Platform fees > 10% of gross revenue
        3. Payout discrepancy > 2%

        Args:
            transactions:   All categorized transactions.
            reconciliation: Pre-computed reconciliation result.

        Returns:
            List of Anomaly objects with human-readable descriptions.
        """
        print("\n[Bookkeeper] Running anomaly detection...")
        raw_anomalies: list[dict] = []

        # Build order lookup by order_id for refund comparison
        order_amounts: dict[str, float] = {
            t.order_id: t.amount_myr
            for t in transactions
            if t.type == TransactionType.ORDER and t.order_id
        }

        # 1. Large refund check
        for txn in transactions:
            if txn.type == TransactionType.REFUND and txn.order_id:
                original = order_amounts.get(txn.order_id)
                if original and original > 0:
                    refund_pct = abs(txn.amount_myr) / original * 100
                    if refund_pct > LARGE_REFUND_THRESHOLD_PCT:
                        raw_anomalies.append({
                            "type": "LARGE_REFUND",
                            "severity": "HIGH",
                            "transaction_id": txn.transaction_id,
                            "detail": (
                                f"Refund TXN {txn.transaction_id} is MYR {abs(txn.amount_myr):.2f}, "
                                f"which is {refund_pct:.1f}% of the original order value "
                                f"MYR {original:.2f} (order {txn.order_id})."
                            ),
                        })

        # 2. High platform fee check
        if reconciliation.gross_sales_myr > 0:
            fee_pct = reconciliation.total_platform_fees_myr / reconciliation.gross_sales_myr * 100
            if fee_pct > HIGH_FEE_THRESHOLD_PCT:
                raw_anomalies.append({
                    "type": "HIGH_PLATFORM_FEE",
                    "severity": "MEDIUM",
                    "transaction_id": None,
                    "detail": (
                        f"Platform fees MYR {reconciliation.total_platform_fees_myr:.2f} "
                        f"are {fee_pct:.2f}% of gross revenue MYR {reconciliation.gross_sales_myr:.2f}, "
                        f"exceeding the {HIGH_FEE_THRESHOLD_PCT}% threshold."
                    ),
                })

        # 3. Payout discrepancy check
        if reconciliation.is_discrepancy_flagged:
            raw_anomalies.append({
                "type": "PAYOUT_DISCREPANCY",
                "severity": "HIGH",
                "transaction_id": None,
                "detail": (
                    f"Actual payout MYR {reconciliation.actual_payout_myr:.2f} differs from "
                    f"expected MYR {reconciliation.expected_payout_myr:.2f} by "
                    f"MYR {reconciliation.discrepancy_myr:.2f} ({reconciliation.discrepancy_pct:.2f}%)."
                ),
            })

        # Ask Claude to write plain-English explanations for each anomaly
        anomalies: List[Anomaly] = []
        for raw in raw_anomalies:
            explanation = self._explain_anomaly(raw)
            anomalies.append(Anomaly(
                type=raw["type"],
                description=explanation,
                severity=raw["severity"],
                related_transaction_id=raw.get("transaction_id"),
            ))
            print(f"  ⚠️  {raw['type']} ({raw['severity']}): {explanation[:80]}...")

        if not anomalies:
            print("  ✅ No anomalies detected.")

        return anomalies

    def _explain_anomaly(self, raw: dict) -> str:
        """
        Ask Claude to write a friendly, plain-English explanation of an anomaly.

        Args:
            raw: dict with 'type', 'severity', and 'detail' keys.

        Returns:
            A 1-2 sentence plain-English explanation suitable for a WhatsApp message.
        """
        prompt = f"""You are Fynn, a friendly AI bookkeeping assistant for e-commerce sellers.
Write a clear, plain-English explanation of the following accounting anomaly.
Keep it to 1-2 sentences. Be helpful, not alarming. Use simple language.

Anomaly type: {raw['type']}
Severity: {raw['severity']}
Technical detail: {raw['detail']}

Respond with only the explanation text, no JSON, no bullet points."""

        explanation = self._call_claude(prompt)

        # Fallback if API is unavailable
        if "error" in explanation.lower() or not explanation.strip():
            return raw["detail"]

        return explanation.strip()
