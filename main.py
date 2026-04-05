"""
Fynn — Autonomous AI Bookkeeping Agent
FastAPI entry point.

Endpoints:
  GET  /health                → health check
  POST /upload/csv            → upload real Shopee Finance CSV
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRouter
from contextlib import asynccontextmanager
from pydantic import BaseModel

from agents.bookkeeper import BookkeeperAgent
from services.conversation import FILE_TYPE_LABELS, ConversationManager
from services.csv_parser import parse_shopee_csv
from services.database import load_all_csvs, load_latest_pnl_any, save_csv
from services.file_classifier import classify_file
from services.scheduler import create_scheduler, set_profile_store


# ── App lifespan — starts/stops scheduler with the server ─────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the scheduler when the server starts, restore state, stop on shutdown."""
    global _last_pnl

    print("\n[Fynn] Starting up...")

    # Restore last P&L from Supabase so /ask works after restarts
    restored = load_latest_pnl_any()
    if restored:
        _last_pnl = restored
        print(f"  [Fynn] Restored last P&L from Supabase ({restored.get('period')})")
    else:
        print("  [Fynn] No previous P&L found in Supabase.")

    # Restore all uploaded CSVs from Supabase
    for row in load_all_csvs():
        try:
            platform = row.get("platform", "shopee")
            file_type = row.get("file_type", "transactions")
            if file_type == "transactions":
                txns = parse_shopee_csv(row["csv_data"].encode("utf-8"))
                _platform_transactions[platform] = txns
                print(f"  [Fynn] Restored {platform} transactions ({row.get('period')}, {len(txns)} rows)")
        except Exception as exc:
            print(f"  [Fynn] Failed to restore CSV {row.get('id')}: {exc}")

    print("[Fynn] Starting scheduler...")
    scheduler = create_scheduler()
    set_profile_store(_conversation.profiles)
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    print("\n[Fynn] Stopping scheduler...")
    scheduler.shutdown()


app = FastAPI(
    title="Fynn Bookkeeping Agent",
    description="Autonomous AI bookkeeping agent for cross-border e-commerce sellers.",
    version="0.1.0",
    lifespan=lifespan,
)

# Shared state
_last_pnl: dict = {}
# Per-platform transaction store: {"shopee": [...], "lazada": [...], ...}
_platform_transactions: dict[str, list] = {}
# Holds the raw bytes of the last unclassified file per sender, pending user clarification
_pending_files: dict[str, bytes] = {}
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


# ── Web settings page ──────────────────────────────────────────────────────────

def _settings_html(phone: str, profile=None, saved: bool = False) -> str:
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    app_url = f"https://{base_url}" if base_url else "http://localhost:8000"

    name     = profile.name if profile and profile.name != "Seller" else ""
    currency = profile.currency if profile else "SGD"
    lang     = profile.language if profile else "en"
    hour     = profile.report_time_hour if profile else 8
    daily    = "checked" if profile and profile.daily_enabled else ""
    weekly   = "checked" if profile and profile.weekly_enabled else ""
    monthly  = "checked" if profile and profile.monthly_enabled else "checked"

    saved_banner = """
    <div style="background:#d1fae5;border:1px solid #6ee7b7;color:#065f46;padding:12px 16px;
                border-radius:8px;margin-bottom:24px;font-weight:600;">
      ✅ Settings saved! Head back to WhatsApp — Fynn is ready.
    </div>""" if saved else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fynn — Your Settings</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#f9fafb;color:#111;min-height:100vh;display:flex;
          align-items:flex-start;justify-content:center;padding:32px 16px}}
    .card{{background:#fff;border-radius:16px;box-shadow:0 2px 16px rgba(0,0,0,.08);
           padding:32px;width:100%;max-width:440px}}
    h1{{font-size:1.5rem;font-weight:700;margin-bottom:4px}}
    .sub{{color:#6b7280;font-size:.9rem;margin-bottom:28px}}
    label{{display:block;font-size:.85rem;font-weight:600;color:#374151;margin-bottom:6px}}
    input[type=text],select{{width:100%;padding:10px 12px;border:1.5px solid #d1d5db;
      border-radius:8px;font-size:1rem;outline:none;transition:border .2s}}
    input[type=text]:focus,select:focus{{border-color:#6366f1}}
    .field{{margin-bottom:20px}}
    .checks{{display:flex;gap:16px;flex-wrap:wrap}}
    .checks label{{display:flex;align-items:center;gap:6px;font-weight:400;
                   font-size:.95rem;cursor:pointer}}
    .checks input{{width:16px;height:16px;accent-color:#6366f1}}
    .hours{{display:flex;align-items:center;gap:10px}}
    .hours input[type=number]{{width:80px}}
    .hours span{{color:#6b7280;font-size:.9rem}}
    button{{width:100%;padding:13px;background:#6366f1;color:#fff;border:none;
            border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;
            margin-top:8px;transition:background .2s}}
    button:hover{{background:#4f46e5}}
    .logo{{font-size:1.1rem;font-weight:800;color:#6366f1;margin-bottom:24px}}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">🤖 Fynn</div>
  <h1>Your Settings</h1>
  <p class="sub">Set up once — Fynn remembers everything.</p>
  {saved_banner}
  <form method="POST" action="{app_url}/settings/save">
    <input type="hidden" name="phone" value="{phone}"/>

    <div class="field">
      <label>Your name</label>
      <input type="text" name="name" value="{name}" placeholder="e.g. Naomi" required/>
    </div>

    <div class="field">
      <label>Report currency</label>
      <select name="currency">
        <option value="SGD" {"selected" if currency=="SGD" else ""}>SGD — Singapore Dollar</option>
        <option value="MYR" {"selected" if currency=="MYR" else ""}>MYR — Malaysian Ringgit</option>
        <option value="USD" {"selected" if currency=="USD" else ""}>USD — US Dollar</option>
      </select>
    </div>

    <div class="field">
      <label>Report language</label>
      <select name="language">
        <option value="en" {"selected" if lang=="en" else ""}>English</option>
        <option value="zh" {"selected" if lang=="zh" else ""}>中文 (Mandarin)</option>
      </select>
    </div>

    <div class="field">
      <label>Report frequency</label>
      <div class="checks">
        <label><input type="checkbox" name="daily_enabled" value="1" {daily}/> Daily ping</label>
        <label><input type="checkbox" name="weekly_enabled" value="1" {weekly}/> Weekly summary</label>
        <label><input type="checkbox" name="monthly_enabled" value="1" {monthly}/> Monthly P&amp;L</label>
      </div>
    </div>

    <div class="field">
      <label>Send reports at</label>
      <div class="hours">
        <input type="number" name="report_time_hour" min="0" max="23" value="{hour}"/>
        <span>:00 (24h, server time)</span>
      </div>
    </div>

    <button type="submit">Save settings →</button>
  </form>
</div>
</body>
</html>"""


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(phone: str = Query(...)) -> HTMLResponse:
    """Render the settings form for a seller, pre-filled if profile exists."""
    profile = _conversation.profiles.get(phone)
    return HTMLResponse(_settings_html(phone, profile))


@app.post("/settings/save", response_class=HTMLResponse)
async def settings_save(
    phone:             str = Form(...),
    name:              str = Form(...),
    currency:          str = Form("SGD"),
    language:          str = Form("en"),
    report_time_hour:  str = Form("8"),
    daily_enabled:     str = Form(None),
    weekly_enabled:    str = Form(None),
    monthly_enabled:   str = Form(None),
) -> HTMLResponse:
    """Save the settings form and send a WhatsApp confirmation."""
    profile, _ = _conversation.profiles.get_or_create(phone)

    profile.name              = name.strip().title()
    profile.currency          = currency
    profile.language          = language
    profile.report_time_hour  = max(0, min(23, int(report_time_hour or 8)))
    profile.daily_enabled     = daily_enabled == "1"
    profile.weekly_enabled    = weekly_enabled == "1"
    profile.monthly_enabled   = monthly_enabled == "1"
    profile.onboarding_step   = None  # mark onboarding complete

    _conversation.profiles.save(profile)

    # Send WhatsApp confirmation
    strings = __import__("services.conversation", fromlist=["ZH", "EN"])
    S = strings.ZH if language == "zh" else strings.EN
    msg = S["complete"].format(name=profile.name, summary=profile.to_summary())
    _twiml_send(phone, msg)

    return HTMLResponse(_settings_html(phone, profile, saved=True))


# ── Upload web page ────────────────────────────────────────────────────────────

@app.get("/upload", response_class=HTMLResponse)
async def upload_page() -> HTMLResponse:
    """Drag-and-drop multi-file upload page."""
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    app_url = f"https://{base_url}" if base_url else "http://localhost:8000"

    platform_badges = {
        "shopee":  ("#ee4d2d", "Shopee"),
        "lazada":  ("#0f146d", "Lazada"),
        "amazon":  ("#ff9900", "Amazon"),
        "shopify": ("#96bf48", "Shopify"),
        "tiktok":  ("#010101", "TikTok Shop"),
        "generic": ("#6366f1", "Internal"),
        "unknown": ("#9ca3af", "Unknown"),
    }
    file_type_icons = {
        "transactions": "🛒", "cogs": "📦", "ads": "📣",
        "warehouse": "🏭", "payroll": "👥", "packaging": "📫",
        "expense": "💸", "unknown": "❓",
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fynn — Upload Files</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#f9fafb;color:#111;min-height:100vh;padding:32px 16px}}
    .wrap{{max-width:640px;margin:0 auto}}
    .logo{{font-size:1.1rem;font-weight:800;color:#6366f1;margin-bottom:8px}}
    h1{{font-size:1.5rem;font-weight:700;margin-bottom:4px}}
    .sub{{color:#6b7280;font-size:.9rem;margin-bottom:28px}}
    .dropzone{{border:2px dashed #d1d5db;border-radius:16px;padding:40px;
               text-align:center;background:#fff;cursor:pointer;transition:all .2s}}
    .dropzone.drag{{border-color:#6366f1;background:#eef2ff}}
    .dropzone-icon{{font-size:2.5rem;margin-bottom:12px}}
    .dropzone p{{color:#6b7280;font-size:.95rem}}
    .dropzone strong{{color:#6366f1;cursor:pointer}}
    #fileInput{{display:none}}
    .file-list{{margin-top:20px;display:flex;flex-direction:column;gap:10px}}
    .file-card{{background:#fff;border-radius:12px;padding:14px 16px;
                box-shadow:0 1px 4px rgba(0,0,0,.08);display:flex;
                align-items:center;gap:12px}}
    .file-card .icon{{font-size:1.4rem}}
    .file-card .info{{flex:1;min-width:0}}
    .file-card .name{{font-weight:600;font-size:.9rem;white-space:nowrap;
                      overflow:hidden;text-overflow:ellipsis}}
    .file-card .meta{{font-size:.78rem;color:#6b7280;margin-top:2px}}
    .badge{{display:inline-block;padding:2px 8px;border-radius:999px;
            font-size:.72rem;font-weight:700;color:#fff;margin-right:4px}}
    .status-pending{{color:#9ca3af}}
    .status-ok{{color:#10b981}}
    .status-err{{color:#ef4444}}
    .btn{{display:block;width:100%;padding:13px;background:#6366f1;color:#fff;
          border:none;border-radius:8px;font-size:1rem;font-weight:600;
          cursor:pointer;margin-top:20px;transition:background .2s}}
    .btn:hover{{background:#4f46e5}}
    .btn:disabled{{background:#c7d2fe;cursor:not-allowed}}
    .result{{margin-top:20px;padding:16px;border-radius:12px;font-size:.9rem;
             background:#d1fae5;border:1px solid #6ee7b7;color:#065f46;display:none}}
    .result.err{{background:#fee2e2;border-color:#fca5a5;color:#991b1b}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="logo">🤖 Fynn</div>
  <h1>Upload your files</h1>
  <p class="sub">Drop any CSV files — Fynn will automatically identify what each one is.<br>
  Supports: Shopee, Lazada, supplier invoices, ads, warehouse costs, payroll, and more.</p>

  <div class="dropzone" id="dropzone">
    <div class="dropzone-icon">📂</div>
    <p>Drag &amp; drop CSV files here<br>or <strong onclick="document.getElementById('fileInput').click()">browse files</strong></p>
  </div>
  <input type="file" id="fileInput" multiple accept=".csv"/>

  <div class="file-list" id="fileList"></div>
  <div class="result" id="result"></div>
  <button class="btn" id="uploadBtn" disabled onclick="uploadAll()">Upload &amp; Classify →</button>
</div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileList  = document.getElementById('fileList');
const uploadBtn = document.getElementById('uploadBtn');
const result    = document.getElementById('result');
let selectedFiles = [];

dropzone.addEventListener('dragover', e => {{ e.preventDefault(); dropzone.classList.add('drag'); }});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', e => {{
  e.preventDefault(); dropzone.classList.remove('drag');
  addFiles([...e.dataTransfer.files]);
}});
fileInput.addEventListener('change', () => addFiles([...fileInput.files]));

function addFiles(files) {{
  files.filter(f => f.name.endsWith('.csv')).forEach(f => {{
    if (!selectedFiles.find(x => x.name === f.name)) selectedFiles.push(f);
  }});
  renderList();
}}

function renderList() {{
  fileList.innerHTML = selectedFiles.map((f, i) => `
    <div class="file-card" id="card-${{i}}">
      <div class="icon">📄</div>
      <div class="info">
        <div class="name">${{f.name}}</div>
        <div class="meta status-pending" id="meta-${{i}}">Waiting to classify...</div>
      </div>
    </div>`).join('');
  uploadBtn.disabled = selectedFiles.length === 0;
}}

async function uploadAll() {{
  uploadBtn.disabled = true;
  result.style.display = 'none';
  const summary = [];

  for (let i = 0; i < selectedFiles.length; i++) {{
    const f = selectedFiles[i];
    const meta = document.getElementById('meta-' + i);
    meta.className = 'meta status-pending';
    meta.textContent = 'Classifying...';

    const fd = new FormData();
    fd.append('file', f);

    try {{
      const res = await fetch('{app_url}/upload/classify', {{method:'POST', body:fd}});
      const data = await res.json();
      if (res.ok) {{
        meta.className = 'meta status-ok';
        meta.innerHTML = `
          <span class="badge" style="background:${{data.platform_color}}">${{data.platform_label}}</span>
          <span class="badge" style="background:#374151">${{data.type_icon}} ${{data.file_type}}</span>
          ${{data.transaction_count ? data.transaction_count + ' rows' : ''}}
          · ${{data.notes}}`;
        summary.push(data);
      }} else {{
        meta.className = 'meta status-err';
        meta.textContent = 'Error: ' + (data.error || 'unknown');
      }}
    }} catch(e) {{
      meta.className = 'meta status-err';
      meta.textContent = 'Upload failed: ' + e.message;
    }}
  }}

  uploadBtn.disabled = false;
  if (summary.length) {{
    result.style.display = 'block';
    result.className = 'result';
    const platforms = [...new Set(summary.map(s => s.platform_label))].join(', ');
    result.innerHTML = `✅ ${{summary.length}} file(s) uploaded and classified (${{platforms}}).<br>
      Head back to WhatsApp and say <strong>run my report</strong> to generate your P&L.`;
  }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.post("/upload/classify")
async def upload_classify(file: UploadFile = File(...)) -> JSONResponse:
    """
    Classify a single uploaded CSV and store it.

    Returns classification result with platform, file_type, and row counts.
    """
    global _platform_transactions

    if not file.filename.endswith(".csv"):
        return JSONResponse(content={"error": "Only .csv files are supported."}, status_code=400)

    content = await file.read()

    # Classify with Claude
    classified = classify_file(file.filename, content)
    platform  = classified.platform
    file_type = classified.file_type

    # Parse and store transaction files
    transaction_count = None
    period = "unknown"
    if file_type == "transactions":
        try:
            txns = parse_shopee_csv(content)
            _platform_transactions[platform] = txns
            transaction_count = len(txns)
            if txns:
                dates = [t.date for t in txns]
                earliest, latest = min(dates), max(dates)
                if earliest.month == latest.month and earliest.year == latest.year:
                    period = earliest.strftime("%B %Y")
                else:
                    period = f"{earliest.strftime('%b %Y')} – {latest.strftime('%b %Y')}"
        except ValueError as exc:
            return JSONResponse(content={"error": str(exc)}, status_code=422)

    # Persist to Supabase
    save_csv(content, period, platform, file_type)

    # Badge colors and icons for the UI
    platform_colors = {
        "shopee": "#ee4d2d", "lazada": "#0f146d", "amazon": "#ff9900",
        "shopify": "#96bf48", "tiktok": "#010101", "generic": "#6366f1", "unknown": "#9ca3af",
    }
    platform_labels = {
        "shopee": "Shopee", "lazada": "Lazada", "amazon": "Amazon",
        "shopify": "Shopify", "tiktok": "TikTok Shop", "generic": "Internal", "unknown": "Unknown",
    }
    type_icons = {
        "transactions": "🛒", "cogs": "📦", "ads": "📣", "warehouse": "🏭",
        "payroll": "👥", "packaging": "📫", "expense": "💸", "unknown": "❓",
    }

    print(f"\n[Upload] {file.filename} → {platform}/{file_type} (confidence: {classified.confidence:.0%})")

    return JSONResponse(content={
        "filename":         file.filename,
        "platform":         platform,
        "platform_label":   platform_labels.get(platform, platform.title()),
        "platform_color":   platform_colors.get(platform, "#9ca3af"),
        "file_type":        file_type,
        "type_icon":        type_icons.get(file_type, "❓"),
        "confidence":       classified.confidence,
        "notes":            classified.notes,
        "period":           period,
        "transaction_count": transaction_count,
    })


# ── Schedule status ────────────────────────────────────────────────────────────

@app.get("/schedule")
async def get_schedule() -> JSONResponse:
    """
    Show all scheduled jobs and their next run times.

    Returns:
        JSON list of jobs with name, id, and next_run_time.
    """
    scheduler: AsyncIOScheduler = app.state.scheduler
    jobs = []
    for job in scheduler.get_jobs():
        next_run = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.isoformat() if next_run else "scheduler not yet started",
        })
    return JSONResponse(content={"jobs": jobs})


@app.post("/schedule/trigger/{job_id}")
async def trigger_job(job_id: str) -> JSONResponse:
    """
    Manually trigger a scheduled job by ID immediately.

    Valid job IDs: daily_ping, weekly_summary, monthly_report

    Args:
        job_id: The scheduler job ID to run now.

    Returns:
        JSON confirmation or error.
    """
    scheduler: AsyncIOScheduler = app.state.scheduler
    job = scheduler.get_job(job_id)
    if not job:
        return JSONResponse(
            content={"error": f"Job '{job_id}' not found. Valid IDs: daily_ping, weekly_summary, monthly_report"},
            status_code=404,
        )
    from datetime import datetime as dt
    job.modify(next_run_time=dt.now())
    return JSONResponse(content={"status": "triggered", "job": job_id})


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

    # Persist P&L to Supabase
    from services.database import save_pnl
    save_pnl("system", _last_pnl)

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

def _run_report_background(sender: str) -> None:
    """Run the full agent pipeline and send results via Twilio when done."""
    global _last_pnl
    try:
        profile = _conversation.profiles.get(sender)
        seller_name = profile.name if profile else "Seller"
        currency = profile.currency if profile else "SGD"
        agent = BookkeeperAgent(seller_name=seller_name, currency=currency)
        result = agent.run(period="March 2026", platform="Shopee MY")
        pnl = result.get("pnl", {})

        _last_pnl = pnl
        _conversation.store_pnl(sender, pnl)

        from services.database import save_pnl
        save_pnl(sender, pnl)

        _twiml_send(sender, "✅ Done! Your March P&L has been sent and your Google Sheet is updated.")
    except Exception as exc:
        print(f"  [Webhook] Background report failed: {exc}")
        _twiml_send(sender, f"❌ Report failed: {exc}")


def _handle_file_background(sender: str, media_url: str, filename: str) -> None:
    """Download, classify, parse and store a file sent via WhatsApp."""
    global _platform_transactions
    try:
        import httpx
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        auth_token  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30)
        resp.raise_for_status()
        content = resp.content

        classified = classify_file(filename, content)
        platform  = classified.platform
        file_type = classified.file_type

        # Detect period
        period = "unknown"
        transaction_count = None

        if file_type == "transactions":
            txns = parse_shopee_csv(content)
            _platform_transactions[platform] = txns
            transaction_count = len(txns)
            if txns:
                dates = [t.date for t in txns]
                e, l = min(dates), max(dates)
                period = e.strftime("%B %Y") if e.month == l.month and e.year == l.year \
                         else f"{e.strftime('%b %Y')} – {l.strftime('%b %Y')}"

        save_csv(content, period, platform, file_type)

        platform_labels = {
            "shopee": "Shopee", "lazada": "Lazada", "amazon": "Amazon",
            "shopify": "Shopify", "tiktok": "TikTok Shop", "generic": "Internal", "unknown": "?",
        }
        type_icons = {
            "transactions": "🛒", "cogs": "📦", "ads": "📣", "warehouse": "🏭",
            "payroll": "👥", "packaging": "📫", "expense": "💸", "unknown": "❓",
        }
        plabel = platform_labels.get(platform, platform.title())
        ticon  = type_icons.get(file_type, "❓")

        if platform == "unknown" or file_type == "unknown" or classified.confidence < 0.5:
            # Store raw bytes so we can re-process once user clarifies
            _pending_files[sender] = content
            reply = (
                f"🤔 I received your file but I'm not sure what it is "
                f"(confidence: {classified.confidence:.0%}).\n\n"
                f"Can you tell me what it contains? Reply with one of:\n"
                f"• *shopee* — Shopee Finance export\n"
                f"• *lazada* — Lazada Finance export\n"
                f"• *cogs* — Supplier/product costs\n"
                f"• *ads* — Advertising spend\n"
                f"• *warehouse* — Storage/fulfilment costs\n"
                f"• *payroll* — Staff costs\n"
                f"• *packaging* — Packaging materials\n"
                f"• *expense* — Other business expense"
            )
        elif file_type == "transactions" and transaction_count:
            reply = (
                f"{ticon} Got it! *{plabel}* finance file received.\n"
                f"📅 Period: {period}\n"
                f"📊 {transaction_count} transactions loaded\n\n"
                f"Send more files, or say *run my report* to generate your P&L. 🚀"
            )
        else:
            reply = (
                f"{ticon} Got it! Logged as *{plabel} — {file_type}*.\n"
                f"I'll factor this into your next report. Send more files or say *run my report*. 📋"
            )

        _twiml_send(sender, reply)

    except Exception as exc:
        print(f"  [Webhook] File handling failed: {exc}")
        _twiml_send(sender, f"❌ Couldn't read that file: {exc}\nMake sure it's a CSV exported from Shopee/Lazada.")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None),
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
    print(f"\n[Webhook] Incoming from {From}: {Body or '[file]'}")

    sender = From.strip()
    message = Body.strip()

    # ── Incoming file ──────────────────────────────────────────────────────────
    if int(NumMedia or 0) > 0 and MediaUrl0:
        content_type = MediaContentType0 or ""
        # Only handle CSV / spreadsheet files
        if "csv" in content_type or "spreadsheet" in content_type or "text/plain" in content_type:
            # Guess filename from URL or content type
            filename = MediaUrl0.split("/")[-1] + ".csv"
            background_tasks.add_task(_handle_file_background, sender, MediaUrl0, filename)
            return _twiml_response(
                "📂 Got your file! Classifying it now... I'll let you know what I found in a moment."
            )
        else:
            return _twiml_response(
                "I can only read CSV files right now. "
                "Export your data as .csv from Shopee/Lazada and send it here."
            )

    # Get profile (creates new one if first contact)
    profile, is_new = _conversation.profiles.get_or_create(sender)

    def _settings_link() -> str:
        base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        app_url = f"https://{base_url}" if base_url else "http://localhost:8000"
        encoded = sender.replace("+", "%2B")
        return f"{app_url}/settings?phone={encoded}"

    # Any settings-related message (reset, change settings, change currency, etc.)
    if message.lower() in {"reset", "clear", "restart"} or _conversation.is_settings_trigger(message, profile):
        _conversation.clear_history(sender)
        link = _settings_link()
        reply = f"Sure! Your current settings are pre-filled — just update what you need:\n{link}"
        return _twiml_response(reply)

    # New user → send settings link
    if not profile.is_onboarding_complete():
        link = _settings_link()
        reply = (
            f"Hey! 👋 I'm *Fynn*, your AI bookkeeper.\n\n"
            f"Set up your preferences here (takes 30 seconds):\n{link}\n\n"
            f"Once done, come back here and say *run my report* to get started!"
        )
        return _twiml_response(reply)

    # Pending file clarification — user is labelling an unknown file
    if sender in _pending_files:
        label = message.strip().lower()
        if label in FILE_TYPE_LABELS:
            raw = _pending_files.pop(sender)
            platform_key, file_type_key = FILE_TYPE_LABELS[label]
            type_icons = {
                "transactions": "🛒", "cogs": "📦", "ads": "📣", "warehouse": "🏭",
                "payroll": "👥", "packaging": "📫", "expense": "💸",
            }
            ticon = type_icons.get(file_type_key, "📄")
            if file_type_key == "transactions":
                try:
                    txns = parse_shopee_csv(raw)
                    _platform_transactions[platform_key] = txns
                    save_csv(raw, "unknown", platform_key, file_type_key)
                    reply = f"{ticon} Got it — saved as *{label.title()}* transactions ({len(txns)} rows). Say *run my report* when ready. 🚀"
                except Exception as exc:
                    reply = f"❌ Couldn't parse that file as transactions: {exc}"
            else:
                save_csv(raw, "unknown", platform_key, file_type_key)
                reply = f"{ticon} Got it — saved as *{file_type_key}*. I'll include it in your next report. 📋"
            return _twiml_response(reply)

    # Onboarded user — check for report trigger
    if _conversation.is_report_trigger(message, profile):
        background_tasks.add_task(_run_report_background, sender)
        return _twiml_response("On it! Running your March 2026 report now... I'll message you here when it's done. ⏳")

    # Conversational Q&A via Claude
    if _last_pnl and not _conversation.get_pnl(sender):
        _conversation.store_pnl(sender, _last_pnl)
    reply = _conversation.handle(sender, message)

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
