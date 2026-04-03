"""
Fynn — Autonomous AI Bookkeeping Agent
FastAPI entry point.

Endpoints:
  GET  /health              → health check
  POST /run-monthly-report  → full Claude agent pipeline
  POST /ask                 → conversational P&L queries
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agents.bookkeeper import BookkeeperAgent

app = FastAPI(
    title="Fynn Bookkeeping Agent",
    description="Autonomous AI bookkeeping agent for cross-border e-commerce sellers.",
    version="0.1.0",
)

# In-memory store for the last P&L — used by /ask
_last_pnl: dict = {}


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
    Trigger the full Fynn agent pipeline for March 2026 (Shopee MY mock data).

    Claude orchestrates all 7 steps via tool calls:
      load_transactions → reconcile_payout → detect_anomalies →
      convert_currency → generate_pnl → write_to_sheets → send_whatsapp

    Returns:
        JSON with full P&L report and pipeline status.
    """
    global _last_pnl

    print("\n" + "=" * 60)
    print("  FYNN — Agent Pipeline Starting")
    print("=" * 60)

    agent = BookkeeperAgent()
    result = agent.run(period="March 2026", platform="Shopee MY")

    _last_pnl = result.get("pnl", {})

    print("\n" + "=" * 60)
    print("  FYNN — Agent Pipeline Complete ✅")
    print("=" * 60 + "\n")

    return JSONResponse(content={"status": "success", **result})


class AskRequest(BaseModel):
    """Request body for the /ask endpoint."""
    question: str


@app.post("/ask")
async def ask(body: AskRequest) -> JSONResponse:
    """
    Answer a natural language question about the last monthly P&L report.

    Claude uses the stored P&L as context to answer questions like:
      - "What was my profit margin?"
      - "How many refunds did I have?"
      - "Were there any anomalies?"
      - "What were my biggest costs?"

    Args:
        body: JSON with a 'question' field.

    Returns:
        JSON with Claude's plain-English answer.
    """
    if not _last_pnl:
        return JSONResponse(
            content={"error": "No report available. Run POST /run-monthly-report first."},
            status_code=400,
        )

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return JSONResponse(
            content={"error": "ANTHROPIC_API_KEY not configured."},
            status_code=500,
        )

    client = anthropic.Anthropic(api_key=api_key)

    system = """You are Fynn, an AI bookkeeping assistant for cross-border e-commerce sellers.
Answer questions about the seller's monthly P&L report clearly and concisely.
Use plain English. Include relevant numbers. Keep answers to 2-4 sentences."""

    prompt = f"""Here is the seller's P&L report:
{json.dumps(_last_pnl, indent=2)}

Seller's question: {body.question}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text
        print(f"\n[Ask] Q: {body.question}")
        print(f"[Ask] A: {answer}")
        return JSONResponse(content={"question": body.question, "answer": answer})
    except Exception as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=500)
