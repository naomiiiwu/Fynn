"""Anomaly Agent — detects unusual patterns in transactions."""

from models.transaction import Anomaly, ReconciliationResult, Transaction, TransactionType

_LARGE_REFUND_PCT  = 20.0
_HIGH_FEE_PCT      = 10.0
_DISCREPANCY_PCT   = 2.0

# Sensitivity multipliers: strict=0.5x thresholds, relaxed=2x
_SENSITIVITY = {
    "strict":  0.5,
    "normal":  1.0,
    "relaxed": 2.0,
}


class AnomalyAgent:
    """Rule-based anomaly detection. No Claude call needed."""

    def run(
        self,
        transactions: list[Transaction],
        reconciliation: ReconciliationResult,
        sensitivity: str = "normal",
    ) -> list[Anomaly]:
        multiplier = _SENSITIVITY.get(sensitivity, 1.0)
        large_refund_threshold  = _LARGE_REFUND_PCT  * multiplier
        high_fee_threshold      = _HIGH_FEE_PCT      * multiplier
        discrepancy_threshold   = _DISCREPANCY_PCT   * multiplier

        anomalies: list[Anomaly] = []

        # Build order amount map for refund ratio calculation
        order_amounts = {
            t.order_id: t.amount_myr
            for t in transactions
            if t.type == TransactionType.ORDER and t.order_id
        }

        # Large refunds
        for t in transactions:
            if t.type == TransactionType.REFUND and t.order_id:
                order_amount = order_amounts.get(t.order_id, 0)
                if order_amount > 0:
                    refund_pct = abs(t.amount_myr) / order_amount * 100
                    if refund_pct > large_refund_threshold:
                        anomalies.append(Anomaly(
                            type="LARGE_REFUND",
                            description=(
                                f"Refund {t.transaction_id} is MYR {abs(t.amount_myr):.2f} "
                                f"({refund_pct:.1f}% of order {t.order_id} value "
                                f"MYR {order_amount:.2f}). "
                                f"This is above the {large_refund_threshold:.0f}% threshold "
                                f"— worth reviewing with the buyer."
                            ),
                            severity="HIGH" if refund_pct > large_refund_threshold * 1.5 else "MEDIUM",
                            related_transaction_id=t.transaction_id,
                        ))

        # High platform fee ratio
        if reconciliation.gross_sales_myr > 0:
            fee_pct = reconciliation.total_platform_fees_myr / reconciliation.gross_sales_myr * 100
            if fee_pct > high_fee_threshold:
                anomalies.append(Anomaly(
                    type="HIGH_PLATFORM_FEE",
                    description=(
                        f"Platform fees are {fee_pct:.1f}% of gross sales "
                        f"(MYR {reconciliation.total_platform_fees_myr:,.2f}). "
                        f"Normal range is below {high_fee_threshold:.0f}%."
                    ),
                    severity="MEDIUM",
                ))

        # Payout discrepancy
        if reconciliation.is_discrepancy_flagged or reconciliation.discrepancy_pct > discrepancy_threshold:
            anomalies.append(Anomaly(
                type="PAYOUT_DISCREPANCY",
                description=(
                    f"Actual payout MYR {reconciliation.actual_payout_myr:,.2f} is "
                    f"MYR {abs(reconciliation.discrepancy_myr):,.2f} "
                    f"({reconciliation.discrepancy_pct:.1f}%) away from expected. "
                    f"Contact Shopee support if this persists."
                ),
                severity="HIGH" if reconciliation.discrepancy_pct > discrepancy_threshold * 2 else "MEDIUM",
            ))

        print(f"  [Anomaly] {len(anomalies)} anomalies detected (sensitivity: {sensitivity})")
        return anomalies
