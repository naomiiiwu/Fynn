"""P&L Agent — builds the final report dict with a Claude-generated narrative."""

from models.transaction import Anomaly, ReconciliationResult, Transaction
from services.currency import CurrencyConverter
from utils.formatter import generate_pnl

from .base import BaseAgent


class PnLAgent(BaseAgent):
    """Generates P&L and adds a plain-English narrative summary."""

    def run(
        self,
        platform: str,
        period: str,
        reconciliation: ReconciliationResult,
        anomalies: list[Anomaly],
        transactions: list[Transaction],
        currency: str = "SGD",
        cost_totals_myr: dict[str, float] | None = None,
    ) -> dict:
        # Currency conversion
        converter = CurrencyConverter()
        conversion = converter.convert(
            reconciliation.actual_payout_myr,
            from_currency="MYR",
            to_currency=currency,
        )

        # Build P&L dict
        pnl = generate_pnl(
            transactions=transactions,
            reconciliation=reconciliation,
            anomalies=anomalies,
            sgd_conversion=conversion,
            period=period,
            platform=platform,
            additional_costs_myr=cost_totals_myr or {},
        )
        pnl["currency"] = currency

        # Ask Claude for a 2-sentence narrative
        pnl["narrative"] = self._generate_narrative(pnl)
        return pnl

    def _generate_narrative(self, pnl: dict) -> str:
        """Ask Claude for a plain-English 2-sentence summary of the month."""
        if not self.client:
            profit = pnl["profit"]["net_profit"]
            currency = pnl.get("currency", "SGD")
            return (
                f"{pnl['period']} report complete. "
                f"Net profit: {currency} {profit:,.2f} "
                f"({pnl['profit']['profit_margin_pct']}% margin)."
            )

        import json
        prompt = (
            f"Write exactly 2 sentences summarising this monthly P&L for a seller. "
            f"Be specific with numbers. Mention any anomalies if present.\n\n"
            f"{json.dumps({k: v for k, v in pnl.items() if k != 'myr_reference'}, indent=2)}"
        )
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            print(f"  [PnL] Narrative generation failed: {exc}")
            return ""
