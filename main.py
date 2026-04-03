"""
Fynn — Autonomous AI Bookkeeping Agent
FastAPI entry point.

Endpoints:
  GET  /health              → health check
  POST /run-monthly-report  → full pipeline execution
"""

import sys
import os

# Add project root to Python path so all internal imports resolve
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agents.bookkeeper import BookkeeperAgent
from data.mock_shopee_data import get_mock_transactions, get_summary
from services.currency import CurrencyConverter
from services.reconciliation import reconcile
from services.sheets import SheetsService
from services.whatsapp import WhatsAppService
from utils.formatter import format_whatsapp_message, generate_pnl, save_pnl_json

app = FastAPI(
    title="Fynn Bookkeeping Agent",
    description="Autonomous AI bookkeeping agent for cross-border e-commerce sellers.",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    """
    Health check endpoint.

    Returns:
        JSON with status and version.
    """
    return {"status": "ok", "version": "0.1.0"}


@app.post("/run-monthly-report")
async def run_monthly_report() -> JSONResponse:
    """
    Trigger the full Fynn pipeline for March 2026 (Shopee MY mock data).

    Pipeline steps:
      1. Load mock Shopee transactions
      2. Reconcile payout (expected vs actual)
      3. AI categorization via Claude
      4. Anomaly detection
      5. Currency conversion (MYR → SGD)
      6. P&L generation
      7. Google Sheets output
      8. WhatsApp summary delivery

    Returns:
        JSON containing the full P&L report and pipeline status.
    """
    print("\n" + "=" * 60)
    print("  FYNN — Monthly Report Pipeline Starting")
    print("=" * 60)

    seller_name = os.getenv("SELLER_NAME", "Seller")
    status_log: dict[str, str] = {}

    # ── Step 1: Load mock data ──────────────────────────────────────
    print("\n[Step 1/7] Loading mock Shopee transaction data...")
    transactions = get_mock_transactions()
    summary = get_summary()
    print(f"  Loaded {summary['total_transactions']} transactions | "
          f"Orders: {summary['orders']} | Refunds: {summary['refunds']} | "
          f"Gross Sales: MYR {summary['gross_sales_myr']:,.2f}")
    status_log["data_load"] = "ok"

    # ── Step 2: Reconcile ───────────────────────────────────────────
    print("\n[Step 2/7] Reconciling payout...")
    reconciliation = reconcile(transactions)
    status_log["reconciliation"] = "ok"

    # ── Step 3: AI Categorization ───────────────────────────────────
    print("\n[Step 3/7] AI transaction categorization...")
    agent = BookkeeperAgent()
    transactions = agent.categorize_all(transactions)
    status_log["categorization"] = "ok"

    # ── Step 4: Anomaly Detection ───────────────────────────────────
    print("\n[Step 4/7] Anomaly detection...")
    anomalies = agent.detect_anomalies(transactions, reconciliation)
    status_log["anomaly_detection"] = "ok"

    # ── Step 5: Currency Conversion ─────────────────────────────────
    print("\n[Step 5/7] Currency conversion (MYR → SGD)...")
    converter = CurrencyConverter()
    sgd_conversion = converter.myr_to_sgd(reconciliation.actual_payout_myr)
    status_log["currency_conversion"] = "ok"

    # ── Step 6: Generate P&L ────────────────────────────────────────
    print("\n[Step 6/7] Generating P&L report...")
    pnl = generate_pnl(
        transactions=transactions,
        reconciliation=reconciliation,
        anomalies=anomalies,
        sgd_conversion=sgd_conversion,
    )
    status_log["pnl_generation"] = "ok"

    # ── Step 7: Google Sheets ───────────────────────────────────────
    print("\n[Step 7/8] Writing to Google Sheets...")
    sheets = SheetsService()
    sheets_ok = sheets.write_pnl(pnl)
    status_log["google_sheets"] = "ok" if sheets_ok else "fallback_json"

    if not sheets_ok:
        save_pnl_json(pnl, "pnl_report.json")

    # ── Step 8: WhatsApp ────────────────────────────────────────────
    print("\n[Step 8/8] Sending WhatsApp summary...")
    wa_message = format_whatsapp_message(pnl, seller_name=seller_name)
    wa_service = WhatsAppService()
    wa_ok = wa_service.send(wa_message)
    status_log["whatsapp"] = "ok" if wa_ok else "failed"

    print("\n" + "=" * 60)
    print("  FYNN — Pipeline Complete ✅")
    print("=" * 60 + "\n")

    return JSONResponse(content={
        "status": "success",
        "pipeline_status": status_log,
        "pnl": pnl,
    })
