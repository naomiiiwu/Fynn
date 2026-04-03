"""
Fynn AI Bookkeeper Agent — real Claude tool-calling agent.

Claude drives the full pipeline by calling tools in sequence:
  load_transactions → reconcile_payout → detect_anomalies →
  convert_currency → generate_pnl → write_to_sheets → send_whatsapp

Claude reasons about each tool result before deciding the next step.
Tokens are consumed for every tool call + reasoning turn.
"""

import json
import os
from typing import Any

import anthropic

from data.mock_shopee_data import get_mock_transactions
from models.transaction import Anomaly, ReconciliationResult, Transaction, TransactionType
from services.currency import CurrencyConverter
from services.reconciliation import reconcile as reconcile_service
from services.sheets import SheetsService
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message, generate_pnl, save_pnl_json

# ── Tool definitions (passed to Claude) ────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "load_transactions",
        "description": "Load all Shopee mock transactions for the period. Always call this first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "Platform name, e.g. 'Shopee MY'"},
                "period": {"type": "string", "description": "Period label, e.g. 'March 2026'"},
            },
            "required": ["platform", "period"],
        },
    },
    {
        "name": "reconcile_payout",
        "description": (
            "Reconcile the payout: sum orders, subtract refunds, fees, shipping, vouchers. "
            "Compare expected vs actual settlement. Flag if discrepancy > 2%."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Detect anomalies: large refunds (>20% of order), high platform fees (>10% of revenue), "
            "payout discrepancy (>2%). Returns a list of flagged issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "convert_currency",
        "description": "Convert the settlement amount from MYR to SGD using live exchange rates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string", "description": "Source currency, e.g. 'MYR'"},
                "to_currency": {"type": "string", "description": "Target currency, e.g. 'SGD'"},
            },
            "required": ["from_currency", "to_currency"],
        },
    },
    {
        "name": "generate_pnl",
        "description": "Generate the full P&L report in the target currency (SGD).",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "platform": {"type": "string"},
            },
            "required": ["period", "platform"],
        },
    },
    {
        "name": "write_to_sheets",
        "description": "Write the P&L report to Google Sheets. Do this before sending WhatsApp.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "send_whatsapp",
        "description": "Send the P&L summary to the seller via WhatsApp. Call this last.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seller_name": {"type": "string", "description": "Seller's name for personalisation"},
            },
            "required": ["seller_name"],
        },
    },
]


class BookkeeperAgent:
    """
    Fynn's AI bookkeeping agent.

    Claude acts as the orchestrator — it calls tools in order, reasons about
    each result, and decides when the job is done. The Python layer only
    executes what Claude asks for.
    """

    def __init__(self) -> None:
        """Initialise the Anthropic client and internal state."""
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None
        self.model = "claude-sonnet-4-6"
        self.seller_name = os.getenv("SELLER_NAME", "Seller").strip()

        # Shared state populated as tools are called
        self._transactions: list[Transaction] = []
        self._reconciliation: ReconciliationResult | None = None
        self._anomalies: list[Anomaly] = []
        self._conversion: dict = {}
        self._pnl: dict = {}
        self._status: dict[str, str] = {}

    # ── Tool executor ───────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, inputs: dict) -> Any:
        """
        Execute a tool by name and return a JSON-serialisable result.

        Args:
            name:   The tool name Claude requested.
            inputs: The arguments Claude passed to the tool.

        Returns:
            A dict result that gets fed back to Claude as a tool result.
        """
        print(f"\n  → [Tool] {name}({json.dumps(inputs) if inputs else ''})")

        if name == "load_transactions":
            self._transactions = get_mock_transactions()
            counts = {t.type.value: 0 for t in self._transactions}
            for t in self._transactions:
                counts[t.type.value] += 1
            gross = sum(t.amount_myr for t in self._transactions if t.type == TransactionType.ORDER)
            return {
                "total_transactions": len(self._transactions),
                "breakdown": counts,
                "gross_sales_myr": round(gross, 2),
                "period": inputs.get("period"),
                "platform": inputs.get("platform"),
            }

        elif name == "reconcile_payout":
            self._reconciliation = reconcile_service(self._transactions)
            return self._reconciliation.model_dump()

        elif name == "detect_anomalies":
            self._anomalies = _detect_anomalies(self._transactions, self._reconciliation)
            return {
                "anomaly_count": len(self._anomalies),
                "anomalies": [a.model_dump() for a in self._anomalies],
            }

        elif name == "convert_currency":
            converter = CurrencyConverter()
            amount = self._reconciliation.actual_payout_myr if self._reconciliation else 0
            self._conversion = converter.convert(
                amount,
                inputs.get("from_currency", "MYR"),
                inputs.get("to_currency", "SGD"),
            )
            return self._conversion

        elif name == "generate_pnl":
            self._pnl = generate_pnl(
                transactions=self._transactions,
                reconciliation=self._reconciliation,
                anomalies=self._anomalies,
                sgd_conversion=self._conversion,
                period=inputs.get("period", "March 2026"),
                platform=inputs.get("platform", "Shopee MY"),
            )
            self._status["pnl_generation"] = "ok"
            return {k: v for k, v in self._pnl.items() if k != "myr_reference"}

        elif name == "write_to_sheets":
            sheets = SheetsService()
            ok = sheets.write_pnl(self._pnl)
            self._status["google_sheets"] = "ok" if ok else "fallback_json"
            if not ok:
                save_pnl_json(self._pnl)
            return {"success": ok, "status": self._status["google_sheets"]}

        elif name == "send_whatsapp":
            seller = inputs.get("seller_name", self.seller_name)
            message = format_whatsapp_message(self._pnl, seller_name=seller)
            wa = WhatsAppService()
            ok = wa.send(message)
            self._status["whatsapp"] = "ok" if ok else "failed"
            return {"success": ok, "message_preview": message[:120] + "..."}

        else:
            return {"error": f"Unknown tool: {name}"}

    # ── Agent loop ──────────────────────────────────────────────────────────────

    def run(self, period: str = "March 2026", platform: str = "Shopee MY") -> dict:
        """
        Run the full bookkeeping pipeline as a Claude tool-calling agent.

        Claude receives a task, calls tools one by one, reasons about each
        result, and stops when all steps are complete.

        Args:
            period:   The reporting period label.
            platform: The platform name.

        Returns:
            dict with 'pnl' and 'pipeline_status' keys.
        """
        if not self.client:
            print("  [Agent] No API key — running in rule-based fallback mode.")
            return self._fallback_run(period, platform)

        print(f"\n[Agent] Starting Claude tool-calling agent for {platform} {period}...")

        system_prompt = f"""You are Fynn, an autonomous AI bookkeeping agent for cross-border e-commerce sellers.

Your job is to run the complete monthly bookkeeping pipeline for a seller by calling the available tools in the correct order.

Always follow this sequence:
1. load_transactions — load the raw data first
2. reconcile_payout — verify the payout matches expected
3. detect_anomalies — find anything unusual
4. convert_currency — convert MYR to SGD
5. generate_pnl — build the P&L report
6. write_to_sheets — save to Google Sheets
7. send_whatsapp — notify the seller

After each tool call, briefly note what you found before calling the next tool.
When all 7 steps are complete, respond with a final JSON summary:
{{"status": "complete", "summary": "<2-sentence plain English summary of the month>"}}"""

        messages = [
            {
                "role": "user",
                "content": (
                    f"Run the monthly bookkeeping report for {self.seller_name}'s "
                    f"{platform} store for {period}."
                ),
            }
        ]

        max_iterations = 15
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            print(f"\n  [Agent] Turn {iteration} — stop_reason: {response.stop_reason}")

            # Add Claude's response to the conversation
            messages.append({"role": "assistant", "content": response.content})

            # Done — Claude has finished
            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        print(f"  [Agent] Final response: {block.text[:200]}")
                        try:
                            final = json.loads(block.text)
                            if final.get("status") == "complete":
                                self._status["summary"] = final.get("summary", "")
                        except Exception:
                            pass
                break

            # Claude wants to call tools
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })

                messages.append({"role": "user", "content": tool_results})

        return {
            "pnl": self._pnl,
            "pipeline_status": self._status,
        }

    def _fallback_run(self, period: str, platform: str) -> dict:
        """
        Rule-based fallback when no Anthropic API key is configured.

        Runs the same steps in fixed order without Claude orchestration.

        Args:
            period:   The reporting period label.
            platform: The platform name.

        Returns:
            dict with 'pnl' and 'pipeline_status' keys.
        """
        print("  [Agent] Running rule-based fallback pipeline...")
        self._execute_tool("load_transactions", {"platform": platform, "period": period})
        self._execute_tool("reconcile_payout", {})
        self._execute_tool("detect_anomalies", {})
        self._execute_tool("convert_currency", {"from_currency": "MYR", "to_currency": "SGD"})
        self._execute_tool("generate_pnl", {"period": period, "platform": platform})
        self._execute_tool("write_to_sheets", {})
        self._execute_tool("send_whatsapp", {"seller_name": self.seller_name})
        return {"pnl": self._pnl, "pipeline_status": self._status}


# ── Anomaly detection (moved here from old bookkeeper, no longer needs Claude) ──

_LARGE_REFUND_PCT = 20.0
_HIGH_FEE_PCT = 10.0
_DISCREPANCY_PCT = 2.0


def _detect_anomalies(
    transactions: list[Transaction],
    reconciliation: ReconciliationResult,
) -> list[Anomaly]:
    """
    Detect anomalies using rule-based checks.

    Args:
        transactions:   All transactions for the period.
        reconciliation: Pre-computed reconciliation result.

    Returns:
        List of Anomaly objects.
    """
    anomalies: list[Anomaly] = []

    order_amounts = {
        t.order_id: t.amount_myr
        for t in transactions
        if t.type == TransactionType.ORDER and t.order_id
    }

    for txn in transactions:
        if txn.type == TransactionType.REFUND and txn.order_id:
            original = order_amounts.get(txn.order_id)
            if original and original > 0:
                pct = abs(txn.amount_myr) / original * 100
                if pct > _LARGE_REFUND_PCT:
                    anomalies.append(Anomaly(
                        type="LARGE_REFUND",
                        severity="HIGH",
                        related_transaction_id=txn.transaction_id,
                        description=(
                            f"Refund {txn.transaction_id} is MYR {abs(txn.amount_myr):.2f} "
                            f"({pct:.1f}% of order {txn.order_id} value MYR {original:.2f}). "
                            f"This is above the 20% threshold — worth reviewing with the buyer."
                        ),
                    ))

    if reconciliation.gross_sales_myr > 0:
        fee_pct = reconciliation.total_platform_fees_myr / reconciliation.gross_sales_myr * 100
        if fee_pct > _HIGH_FEE_PCT:
            anomalies.append(Anomaly(
                type="HIGH_PLATFORM_FEE",
                severity="MEDIUM",
                related_transaction_id=None,
                description=(
                    f"Platform fees are {fee_pct:.1f}% of gross revenue — "
                    f"above the expected 5–8% range. Check if any fee categories changed."
                ),
            ))

    if reconciliation.is_discrepancy_flagged:
        anomalies.append(Anomaly(
            type="PAYOUT_DISCREPANCY",
            severity="HIGH",
            related_transaction_id=None,
            description=(
                f"Actual payout MYR {reconciliation.actual_payout_myr:.2f} is "
                f"MYR {abs(reconciliation.discrepancy_myr):.2f} "
                f"({reconciliation.discrepancy_pct:.1f}%) away from expected. "
                f"Contact Shopee support if this persists."
            ),
        ))

    return anomalies
