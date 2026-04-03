"""
Fynn — Autonomous AI Bookkeeping Agent
FastAPI entry point.

Endpoints:
  GET  /health                → health check
  POST /run-monthly-report    → full Claude agent pipeline
  POST /ask                   → conversational P&L queries (JSON)
  POST /webhook/whatsapp      → Twilio WhatsApp incoming message webhook
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import anthropic
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from agents.bookkeeper import BookkeeperAgent
from services.conversation import ConversationManager

app = FastAPI(
    title="Fynn Bookkeeping Agent",
    description="Autonomous AI bookkeeping agent for cross-border e-commerce sellers.",
    version="0.1.0",
)

# Shared state
_last_pnl: dict = {}
_conversation = ConversationManager()


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    """
    Health check endpoint.

    Returns:
        JSON with status and version.
    """
    return {"status": "ok", "version": "0.1.0"}


# ── Monthly report pipeline ────────────────────────────────────────────────────

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


# ── JSON ask endpoint (for API/testing use) ────────────────────────────────────

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


# ── WhatsApp webhook ───────────────────────────────────────────────────────────

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
) -> Response:
    """
    Twilio webhook — receives incoming WhatsApp messages and replies as Fynn.

    Twilio POSTs form data here whenever a seller sends a message.
    This endpoint must be publicly accessible (use ngrok for local dev).

    Routing logic:
      - "run report" / "run my report" → triggers full pipeline, replies with status
      - "reset" → clears conversation history
      - anything else → Claude replies as Fynn with P&L context

    Args:
        From: Sender's WhatsApp number (e.g. 'whatsapp:+6591234567').
        Body: The message text.

    Returns:
        TwiML XML response that Twilio uses to send the reply.
    """
    print(f"\n[Webhook] Incoming from {From}: {Body}")

    sender = From.strip()
    message = Body.strip()

    # Reset conversation
    if message.lower() in {"reset", "clear", "restart"}:
        _conversation.clear_history(sender)
        reply = "Conversation reset! 🔄 How can I help you?"

    # Report trigger — run full pipeline then reply
    elif _conversation.is_report_trigger(message):
        reply = "On it! Running your March 2026 report now... I'll update you here when it's done. ⏳"
        # Send immediate acknowledgement, then run pipeline async
        # For MVP: run inline (blocks for ~5-10s), fine for demo
        _twiml_send(sender, reply)

        agent = BookkeeperAgent()
        result = agent.run(period="March 2026", platform="Shopee MY")
        pnl = result.get("pnl", {})

        global _last_pnl
        _last_pnl = pnl
        _conversation.store_pnl(sender, pnl)

        # Pipeline sends its own WhatsApp summary, so just confirm here
        reply = "✅ Done! Your March P&L has been sent and your Google Sheet is updated."

    # Conversational Q&A
    else:
        # Give Claude the latest P&L if we have it
        if _last_pnl and not _conversation.get_pnl(sender):
            _conversation.store_pnl(sender, _last_pnl)
        reply = _conversation.reply(sender, message)

    return _twiml_response(reply)


def _twiml_response(message: str) -> Response:
    """
    Wrap a reply string in TwiML XML so Twilio sends it as a WhatsApp message.

    Args:
        message: The plain-text reply to send.

    Returns:
        FastAPI Response with TwiML content-type.
    """
    # Escape XML special characters
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{safe}</Message>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _twiml_send(to: str, message: str) -> None:
    """
    Send an out-of-band WhatsApp message via Twilio REST API.

    Used to send the immediate acknowledgement before the pipeline runs.

    Args:
        to:      Recipient WhatsApp number (e.g. 'whatsapp:+6591234567').
        message: Message text to send.
    """
    try:
        from twilio.rest import Client
        client = Client(
            os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        )
        client.messages.create(
            from_=os.getenv("TWILIO_WHATSAPP_FROM", "").strip(),
            to=to,
            body=message,
        )
    except Exception as exc:
        print(f"  [Webhook] Out-of-band send failed: {exc}")
