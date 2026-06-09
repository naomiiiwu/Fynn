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
from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRouter
from contextlib import asynccontextmanager
from pydantic import BaseModel

from agents.orchestrator import OrchestratorAgent
from services.conversation import FILE_TYPE_LABELS, ConversationManager
from services.csv_parser import detect_cost_period, parse_cost_total, parse_csv
from services.database import load_all_csvs, load_latest_pnl_any, save_csv
from services.file_classifier import ClassifiedFile, classify_file
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
    _cost_types = {"cogs", "ads", "warehouse", "payroll", "packaging", "expense"}
    for row in load_all_csvs():
        try:
            platform  = row.get("platform", "shopee")
            file_type = row.get("file_type", "transactions")
            raw_bytes = row["csv_data"].encode("utf-8")
            if file_type == "transactions":
                txns = parse_csv(raw_bytes, platform)
                _platform_transactions[platform] = txns
                print(f"  [Fynn] Restored {platform} transactions ({row.get('period')}, {len(txns)} rows)")
            elif file_type in _cost_types:
                period = row.get("period") or detect_cost_period(raw_bytes)
                total  = parse_cost_total(raw_bytes, file_type)
                _cost_totals.setdefault(period, {})[file_type] = total
                print(f"  [Fynn] Restored {file_type} cost for {period}: MYR {total:,.2f}")
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
# Cost totals keyed by period then type: {"March 2026": {"cogs": 8680.0, "payroll": 14540.0}}
_cost_totals: dict[str, dict[str, float]] = {}
# Period of the last completed report — used to warn on stale uploads
_last_report_period: str = ""
# Holds the raw bytes of the last unclassified file per sender, pending user clarification
_pending_files: dict[str, dict] = {}
# Holds reconciliation requests waiting for the user's "proceed anyway" confirmation
_pending_reconciliations: dict[str, dict] = {}
_conversation = ConversationManager()

STRICT_CONFIDENCE_THRESHOLD = 0.90
_COST_FILE_TYPES = {"cogs", "ads", "warehouse", "payroll", "packaging", "expense"}
_SUPPORTED_TRANSACTION_PLATFORMS = {"shopee", "lazada"}
_SUPPORTED_PLATFORMS = ["shopee", "lazada"]
_FILE_LABELS = {
    "shopee": "Shopee finance export",
    "lazada": "Lazada finance export",
    "cogs": "COGS / supplier costs",
    "ads": "Ads spend",
    "warehouse": "Warehouse / 3PL costs",
    "payroll": "Payroll",
    "packaging": "Packaging materials",
    "expense": "Other business expenses",
}

_TEMPLATE_GROUPS: dict[str, list[list[str]]] = {
    "transactions": [
        ["Transaction Date", "Date", "Created At", "Created Time", "Create Time"],
        ["Type", "Transaction Type", "Description", "Remarks"],
        ["Amount", "Total Amount", "Amount (MYR)", "Credit/Debit", "Credit", "Debit"],
    ],
    "cogs": [
        ["Supplier", "Supplier Name", "SKU", "Product Name", "Invoice No.", "Purchase Order"],
        ["Total Cost (MYR)", "Total (MYR)", "Amount (MYR)", "Total Cost", "Amount"],
    ],
    "ads": [
        ["Campaign", "Campaign Name", "Ad Spend", "Spend", "Impressions", "Clicks"],
        ["Ad Spend (MYR)", "Spend (MYR)", "Amount (MYR)", "Ad Spend", "Spend", "Amount"],
    ],
    "warehouse": [
        ["3PL Provider", "Service Type", "Storage", "Fulfilment", "Fulfillment", "Pick and Pack"],
        ["Amount (MYR)", "Total (MYR)", "Amount", "Total"],
    ],
    "payroll": [
        ["Employee", "Employee Name", "Basic Pay", "Salary", "EPF", "SOCSO", "Headcount"],
        ["Total Cost (MYR)", "Amount (MYR)", "Total Cost", "Amount"],
    ],
    "packaging": [
        ["Item", "Material", "Product", "Packaging", "Poly Mailer", "Carton", "Tape"],
        ["Total (MYR)", "Total Cost (MYR)", "Amount (MYR)", "Total", "Amount"],
    ],
    "expense": [
        ["Category", "Description", "Vendor", "Expense", "Subscription", "Utilities", "Rental"],
        ["Amount (MYR)", "Total (MYR)", "Amount", "Total"],
    ],
}


def _canonical_whatsapp_phone(phone: str) -> str:
    """Convert a public phone value into Twilio's WhatsApp sender format."""
    cleaned = (phone or "").strip()
    if cleaned.startswith("whatsapp:"):
        return cleaned
    if cleaned.startswith("+"):
        return f"whatsapp:{cleaned}"
    if cleaned.isdigit():
        return f"whatsapp:+{cleaned}"
    return cleaned


def _public_phone_param(phone: str) -> str:
    """Return the clean phone value shown in setup links."""
    cleaned = (phone or "").strip()
    if cleaned.startswith("whatsapp:+"):
        return cleaned.removeprefix("whatsapp:+")
    if cleaned.startswith("+"):
        return cleaned.removeprefix("+")
    return cleaned


def _app_url() -> str:
    """Return the public app URL, falling back to local dev."""
    base_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    return f"https://{base_url}" if base_url else "http://localhost:8000"


def _setup_link_for_sender(sender: str) -> str:
    """Return the clean setup link for a WhatsApp sender."""
    return f"{_app_url()}/setup?phone={_public_phone_param(sender)}"


def _has_any_header(headers: list[str], candidates: list[str]) -> bool:
    """Check whether uploaded headers include any candidate column name."""
    normalized = [header.strip().lower() for header in headers]
    for candidate in candidates:
        needle = candidate.strip().lower()
        if any(needle == header or needle in header for header in normalized):
            return True
    return False


def _read_csv_headers(content: bytes) -> list[str]:
    """Read CSV headers from uploaded bytes."""
    import csv
    import io

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or [])


def _validate_file_template(classified) -> list[str]:
    """Return validation errors for unsupported or structurally unsafe files."""
    platform = classified.platform
    file_type = classified.file_type
    headers = classified.headers or []

    if file_type == "unknown":
        return ["Fynn could not identify a supported file type from the headers."]

    if file_type == "transactions" and platform == "unknown":
        return ["Fynn could not identify a supported marketplace for this transaction file."]

    if file_type == "transactions" and platform not in _SUPPORTED_TRANSACTION_PLATFORMS:
        return [f"{platform.title()} transaction files are detected but not supported by a parser yet."]

    if file_type not in _TEMPLATE_GROUPS:
        return [f"{file_type} files are not supported yet."]

    missing = []
    for group in _TEMPLATE_GROUPS[file_type]:
        if not _has_any_header(headers, group):
            missing.append(" / ".join(group[:3]))

    if missing:
        return [
            "Missing required column group(s): " + "; ".join(missing),
            "Detected headers: " + ", ".join(headers[:12]),
        ]

    return []


def _period_from_transactions(txns: list) -> str:
    """Return the report period represented by parsed transactions."""
    dates = [t.date for t in txns]
    earliest, latest = min(dates), max(dates)
    if earliest.month == latest.month and earliest.year == latest.year:
        return earliest.strftime("%B %Y")
    return f"{earliest.strftime('%b %Y')} – {latest.strftime('%b %Y')}"


def _inspect_upload(filename: str, content: bytes, classified) -> dict:
    """Validate and parse an upload without saving it into report state."""
    errors = _validate_file_template(classified)
    if errors:
        return {"ok": False, "errors": errors}

    platform = classified.platform
    file_type = classified.file_type
    period = "unknown"
    txns = None
    total = None
    transaction_count = None

    try:
        if file_type == "transactions":
            txns = parse_csv(content, platform)
            transaction_count = len(txns)
            period = _period_from_transactions(txns) if txns else "unknown"
        elif file_type in _COST_FILE_TYPES:
            period = detect_cost_period(content)
            total = parse_cost_total(content, file_type)
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}

    return {
        "ok": True,
        "filename": filename,
        "platform": platform,
        "file_type": file_type,
        "period": period,
        "txns": txns,
        "total": total,
        "transaction_count": transaction_count,
    }


def _save_inspected_upload(content: bytes, inspected: dict) -> None:
    """Save an already-inspected upload into memory and Supabase."""
    platform = inspected["platform"]
    file_type = inspected["file_type"]
    period = inspected["period"]

    if file_type == "transactions":
        _platform_transactions[platform] = inspected["txns"] or []
    elif file_type in _COST_FILE_TYPES:
        _cost_totals.setdefault(period, {})[file_type] = inspected["total"] or 0.0

    save_csv(content, period, platform, file_type)


def _classification_preview(classified, inspected: dict) -> str:
    """Human-readable preview for confirmation before saving."""
    platform_labels = {
        "shopee": "Shopee", "lazada": "Lazada", "amazon": "Amazon",
        "shopify": "Shopify", "tiktok": "TikTok Shop", "generic": "Internal",
    }
    type_labels = {
        "transactions": "Finance Export",
        "cogs": "Supplier/Product Costs",
        "ads": "Advertising Spend",
        "warehouse": "Warehouse/3PL Costs",
        "payroll": "Payroll Costs",
        "packaging": "Packaging Costs",
        "expense": "Business Expense",
    }
    platform = platform_labels.get(inspected["platform"], inspected["platform"].title())
    file_type = type_labels.get(inspected["file_type"], inspected["file_type"].title())
    detail = ""
    if inspected.get("transaction_count") is not None:
        detail = f"\nRows: {inspected['transaction_count']}"
    elif inspected.get("total") is not None:
        detail = f"\nTotal: MYR {inspected['total']:,.2f}"

    return (
        f"I detected this as: *{platform} {file_type}*\n"
        f"Period: {inspected['period']}"
        f"{detail}\n"
        f"Confidence: {classified.confidence:.0%}"
    )


def _pending_upload_options() -> str:
    """Return supported manual labels for pending uploads."""
    return (
        "Reply *confirm* to save it, or correct me with one of:\n"
        "• *shopee* — Shopee Finance export\n"
        "• *lazada* — Lazada Finance export\n"
        "• *cogs* — Supplier/product costs\n"
        "• *ads* — Advertising spend\n"
        "• *warehouse* — Storage/fulfilment costs\n"
        "• *payroll* — Staff costs\n"
        "• *packaging* — Packaging materials\n"
        "• *expense* — Other business expense\n\n"
        "Reply *cancel* to discard the file."
    )


def _onboarding_file_checklist(profile) -> str:
    """Return the file list Fynn expects after onboarding."""
    lines = [
        f"• {_FILE_LABELS.get(platform, platform)}"
        for platform in (profile.platforms or ["shopee"])
    ]
    lines.extend(
        f"• {_FILE_LABELS.get(file_type, file_type)}"
        for file_type in (profile.required_cost_files or [])
    )
    if not lines:
        return "Send your marketplace finance export here in WhatsApp."
    return "Please send these CSV files here in WhatsApp:\n" + "\n".join(lines)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/debug")
async def debug() -> dict:
    """Live state snapshot — use this to diagnose report hangs."""
    return {
        "platform_transactions": {
            platform: len(txns)
            for platform, txns in _platform_transactions.items()
        },
        "cost_totals": {
            period: {ftype: f"MYR {amt:,.2f}" for ftype, amt in costs.items()}
            for period, costs in _cost_totals.items()
        },
        "last_report_period": _last_report_period,
        "last_pnl_period": _last_pnl.get("period") if _last_pnl else None,
        "active_users": list(_conversation.profiles._profiles.keys()),
        "pending_files": list(_pending_files.keys()),
    }


# ── Web setup/preferences page ─────────────────────────────────────────────────

def _settings_html(phone: str, profile=None, saved: bool = False) -> str:
    app_url = _app_url()

    name     = profile.name if profile and profile.name != "Seller" else ""
    currency = profile.currency if profile else "SGD"
    lang     = profile.language if profile else "en"
    hour     = profile.report_time_hour if profile else 8
    daily    = "checked" if profile and profile.daily_enabled else ""
    weekly   = "checked" if profile and profile.weekly_enabled else ""
    monthly  = "checked" if profile and profile.monthly_enabled else "checked"
    platforms = set(profile.platforms if profile else ["shopee"])
    required_files = set(profile.required_cost_files if profile else ["cogs", "ads"])

    saved_banner = """
    <div style="background:#d1fae5;border:1px solid #6ee7b7;color:#065f46;padding:12px 16px;
                border-radius:8px;margin-bottom:24px;font-weight:600;">
      Preferences saved! Head back to WhatsApp — Fynn is ready.
    </div>""" if saved else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fynn — Setup</title>
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
  <h1>Set Up Fynn</h1>
  <p class="sub">Tell Fynn how to prepare your reports.</p>
  {saved_banner}
  <form method="POST" action="{app_url}/setup/save">
    <input type="hidden" name="phone" value="{phone}"/>

    <div class="field">
      <label>What should Fynn call you?</label>
      <input type="text" name="name" value="{name}" placeholder="e.g. Naomi" required/>
    </div>

    <div class="field">
      <label>Which currency should reports use?</label>
      <select name="currency">
        <option value="SGD" {"selected" if currency=="SGD" else ""}>SGD — Singapore Dollar</option>
        <option value="MYR" {"selected" if currency=="MYR" else ""}>MYR — Malaysian Ringgit</option>
        <option value="USD" {"selected" if currency=="USD" else ""}>USD — US Dollar</option>
      </select>
    </div>

    <div class="field">
      <label>Which language do you prefer?</label>
      <select name="language">
        <option value="en" {"selected" if lang=="en" else ""}>English</option>
        <option value="zh" {"selected" if lang=="zh" else ""}>中文 (Mandarin)</option>
      </select>
    </div>

    <div class="field">
      <label>Which marketplaces should Fynn reconcile?</label>
      <div class="checks">
        <label><input type="checkbox" name="platforms" value="shopee" {"checked" if "shopee" in platforms else ""}/> Shopee</label>
        <label><input type="checkbox" name="platforms" value="lazada" {"checked" if "lazada" in platforms else ""}/> Lazada</label>
      </div>
    </div>

    <div class="field">
      <label>Which supporting files should Fynn expect?</label>
      <div class="checks">
        <label><input type="checkbox" name="required_cost_files" value="cogs" {"checked" if "cogs" in required_files else ""}/> COGS</label>
        <label><input type="checkbox" name="required_cost_files" value="ads" {"checked" if "ads" in required_files else ""}/> Ads spend</label>
        <label><input type="checkbox" name="required_cost_files" value="warehouse" {"checked" if "warehouse" in required_files else ""}/> Warehouse</label>
        <label><input type="checkbox" name="required_cost_files" value="payroll" {"checked" if "payroll" in required_files else ""}/> Payroll</label>
        <label><input type="checkbox" name="required_cost_files" value="packaging" {"checked" if "packaging" in required_files else ""}/> Packaging</label>
        <label><input type="checkbox" name="required_cost_files" value="expense" {"checked" if "expense" in required_files else ""}/> Other expenses</label>
      </div>
    </div>

    <div class="field">
      <label>What should Fynn send you?</label>
      <div class="checks">
        <label><input type="checkbox" name="daily_enabled" value="1" {daily}/> Daily reminder</label>
        <label><input type="checkbox" name="weekly_enabled" value="1" {weekly}/> Weekly summary</label>
        <label><input type="checkbox" name="monthly_enabled" value="1" {monthly}/> Monthly P&amp;L</label>
      </div>
    </div>

    <div class="field">
      <label>Best time to receive reports</label>
      <div class="hours">
        <input type="number" name="report_time_hour" min="0" max="23" value="{hour}"/>
        <span>:00 daily, 24-hour time</span>
      </div>
    </div>

    <button type="submit">Save preferences</button>
  </form>
</div>
</body>
</html>"""


@app.get("/setup", response_class=HTMLResponse)
async def settings_page(phone: str = Query(...)) -> HTMLResponse:
    """Render the setup form for a seller, pre-filled if profile exists."""
    canonical_phone = _canonical_whatsapp_phone(phone)
    profile = _conversation.profiles.get(canonical_phone)
    return HTMLResponse(_settings_html(canonical_phone, profile))


@app.get("/settings", response_class=HTMLResponse)
async def legacy_settings_page(phone: str = Query(...)) -> HTMLResponse:
    """Keep existing settings links working."""
    return await settings_page(phone)


@app.post("/setup/save", response_class=HTMLResponse)
async def settings_save(
    background_tasks: BackgroundTasks,
    phone:             str = Form(...),
    name:              str = Form(...),
    currency:          str = Form("SGD"),
    language:          str = Form("en"),
    report_time_hour:  str = Form("8"),
    daily_enabled:     str = Form(None),
    weekly_enabled:    str = Form(None),
    monthly_enabled:   str = Form(None),
    platforms:          list[str] = Form(["shopee"]),
    required_cost_files: list[str] = Form([]),
) -> HTMLResponse:
    """Save the setup form and send a WhatsApp confirmation."""
    phone = _canonical_whatsapp_phone(phone)
    profile, _ = _conversation.profiles.get_or_create(phone)

    profile.name              = name.strip().title()
    profile.currency          = currency
    profile.language          = language
    profile.report_time_hour  = max(0, min(23, int(report_time_hour or 8)))
    profile.daily_enabled     = daily_enabled == "1"
    profile.weekly_enabled    = weekly_enabled == "1"
    profile.monthly_enabled   = monthly_enabled == "1"
    profile.platforms         = [p for p in platforms if p in _SUPPORTED_PLATFORMS] or ["shopee"]
    profile.required_cost_files = [f for f in required_cost_files if f in _COST_FILE_TYPES]
    profile.onboarding_step   = None  # mark onboarding complete

    _conversation.profiles.save(profile)

    # Send WhatsApp confirmation + guide
    from services.conversation import EN, ZH, GUIDE_EN, GUIDE_ZH
    S = ZH if language == "zh" else EN
    msg = (
        S["complete"].format(name=profile.name, summary=profile.to_summary())
        + "\n\n"
        + _onboarding_file_checklist(profile)
    )
    _twiml_send(phone, msg)
    guide = GUIDE_ZH if language == "zh" else GUIDE_EN
    _twiml_send(phone, guide)
    if _has_uploaded_transactions():
        background_tasks.add_task(_auto_refresh_report, phone)

    return HTMLResponse(_settings_html(phone, profile, saved=True))


@app.post("/settings/save", response_class=HTMLResponse)
async def legacy_settings_save(
    background_tasks: BackgroundTasks,
    phone:             str = Form(...),
    name:              str = Form(...),
    currency:          str = Form("SGD"),
    language:          str = Form("en"),
    report_time_hour:  str = Form("8"),
    daily_enabled:     str = Form(None),
    weekly_enabled:    str = Form(None),
    monthly_enabled:   str = Form(None),
    platforms:          list[str] = Form(["shopee"]),
    required_cost_files: list[str] = Form([]),
) -> HTMLResponse:
    """Keep existing settings form submissions working."""
    return await settings_save(
        background_tasks=background_tasks,
        phone=phone,
        name=name,
        currency=currency,
        language=language,
        report_time_hour=report_time_hour,
        daily_enabled=daily_enabled,
        weekly_enabled=weekly_enabled,
        monthly_enabled=monthly_enabled,
        platforms=platforms,
        required_cost_files=required_cost_files,
    )


# ── Upload web page ────────────────────────────────────────────────────────────

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(phone: str = Query("")) -> HTMLResponse:
    """Drag-and-drop multi-file upload page."""
    app_url = _app_url()
    canonical_phone = _canonical_whatsapp_phone(phone) if phone else ""

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
const phone     = {json.dumps(canonical_phone)};
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
    if (phone) fd.append('phone', phone);

    try {{
      const res = await fetch('{app_url}/upload/classify', {{method:'POST', body:fd}});
      const data = await res.json();
      if (res.ok) {{
        if (data.requires_confirmation) {{
          meta.className = 'meta status-pending';
          meta.textContent = data.message + ' Upload via WhatsApp to confirm this file.';
        }} else {{
          meta.className = 'meta status-ok';
          meta.innerHTML = `
            <span class="badge" style="background:${{data.platform_color}}">${{data.platform_label}}</span>
            <span class="badge" style="background:#374151">${{data.type_icon}} ${{data.file_type}}</span>
            ${{data.transaction_count ? data.transaction_count + ' rows' : ''}}
            · ${{data.notes}}`;
        }}
        summary.push(data);
      }} else {{
        meta.className = 'meta status-err';
        const details = data.details ? ' ' + data.details.join(' ') : '';
        meta.textContent = 'Error: ' + (data.error || 'unknown') + details;
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
    const platforms = [...new Set(summary.map(s => s.platform_label).filter(Boolean))].join(', ') || 'needs confirmation';
    const refreshStarted = summary.some(s => s.auto_refresh);
    const needsConfirmation = summary.some(s => s.requires_confirmation);
    result.innerHTML = refreshStarted
      ? `✅ ${{summary.length}} file(s) uploaded and classified (${{platforms}}).<br>
         Fynn is refreshing your report now.`
      : needsConfirmation
        ? `⚠️ ${{summary.length}} file(s) checked. Some need confirmation before Fynn can save them.`
        : `✅ ${{summary.length}} file(s) uploaded and classified (${{platforms}}).`;
  }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.post("/upload/classify")
async def upload_classify(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    phone: str = Form(""),
) -> JSONResponse:
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

    inspected = _inspect_upload(file.filename, content, classified)
    if not inspected["ok"]:
        return JSONResponse(
            content={
                "error": "File does not match an accepted Fynn template.",
                "details": inspected["errors"],
                "platform": platform,
                "file_type": file_type,
                "confidence": classified.confidence,
            },
            status_code=422,
        )

    if classified.confidence < STRICT_CONFIDENCE_THRESHOLD:
        return JSONResponse(content={
            "filename":              file.filename,
            "platform":              platform,
            "file_type":             file_type,
            "confidence":            classified.confidence,
            "period":                inspected["period"],
            "transaction_count":     inspected["transaction_count"],
            "requires_confirmation": True,
            "preview":               _classification_preview(classified, inspected),
            "message":               "Classification confidence is below 90%, so Fynn did not save this file yet.",
        })

    _save_inspected_upload(content, inspected)

    auto_refresh = bool(phone and file_type != "unknown")
    if auto_refresh:
        background_tasks.add_task(_auto_refresh_report, _canonical_whatsapp_phone(phone))

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
        "period":           inspected["period"],
        "transaction_count": inspected["transaction_count"],
        "requires_confirmation": False,
        "auto_refresh":     auto_refresh,
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

def _detect_period() -> str:
    """Detect reporting period from uploaded transactions, fallback to current month."""
    from datetime import datetime as dt
    for txns in _platform_transactions.values():
        if txns:
            dates = [t.date for t in txns]
            e = min(dates)
            return e.strftime("%B %Y")
    return dt.now().strftime("%B %Y")


def _has_uploaded_transactions() -> bool:
    """Return True when at least one real platform transaction CSV is loaded."""
    return any(
        platform != "unknown" and bool(txns)
        for platform, txns in _platform_transactions.items()
    )


def _cost_file_uploaded(file_type: str) -> bool:
    """Return True if any uploaded period contains the requested cost file."""
    return any(file_type in costs for costs in _cost_totals.values())


def _missing_reconciliation_requirements(profile) -> tuple[list[str], list[str]]:
    """Return missing essential finance exports and optional supporting files."""
    missing_essential = [
        platform
        for platform in (profile.platforms or ["shopee"])
        if not _platform_transactions.get(platform)
    ]
    missing_support = [
        file_type
        for file_type in (profile.required_cost_files or [])
        if not _cost_file_uploaded(file_type)
    ]
    return missing_essential, missing_support


def _format_missing_files(missing_essential: list[str], missing_support: list[str]) -> str:
    """Format missing upload requirements for WhatsApp."""
    lines = []
    if missing_essential:
        lines.append("*Required before reconciliation:*")
        lines.extend(f"• {_FILE_LABELS.get(item, item)}" for item in missing_essential)
    if missing_support:
        lines.append("*Still missing from your onboarding plan:*")
        lines.extend(f"• {_FILE_LABELS.get(item, item)}" for item in missing_support)
    return "\n".join(lines)


def _auto_refresh_report(sender: str, force: bool = False) -> None:
    """Refresh the report after a successful upload, if reconciliation data exists."""
    profile = _conversation.profiles.get(sender)
    if not profile or not profile.is_onboarding_complete():
        _twiml_send(
            sender,
            "Saved. Complete setup first so I know which marketplaces and files to reconcile.\n"
            f"Setup link: {_setup_link_for_sender(sender)}",
        )
        return

    if not _has_uploaded_transactions():
        _twiml_send(
            sender,
            "Saved. I’ll refresh your report once you send a Shopee/Lazada finance export.",
        )
        return

    missing_essential, missing_support = _missing_reconciliation_requirements(profile)
    if missing_essential:
        _pending_reconciliations[sender] = {
            "missing_essential": missing_essential,
            "missing_support": missing_support,
        }
        _twiml_send(
            sender,
            "I saved the file, but I can’t reconcile yet because a marketplace finance export is missing.\n\n"
            + _format_missing_files(missing_essential, missing_support),
        )
        return

    if missing_support and not force:
        _pending_reconciliations[sender] = {
            "missing_essential": [],
            "missing_support": missing_support,
        }
        _twiml_send(
            sender,
            "I can refresh your reconciliation now, but some supporting files from onboarding are missing.\n\n"
            + _format_missing_files([], missing_support)
            + "\n\nReply *proceed* to reconcile with available files, or send the missing files first.",
        )
        return

    _pending_reconciliations.pop(sender, None)
    _run_report_background(sender, allow_mock=False)


def _run_report_background(sender: str, allow_mock: bool = True) -> None:
    """Run the full multi-agent pipeline and send results via Twilio when done."""
    global _last_pnl, _last_report_period
    from datetime import datetime as _dt

    def _log(msg: str) -> None:
        print(f"  [Report:{_dt.now().strftime('%H:%M:%S')}] {msg}")

    try:
        _log("Starting pipeline...")

        if not allow_mock and not _has_uploaded_transactions():
            _log("Skipped auto-refresh: no uploaded transactions available.")
            return

        profile = _conversation.profiles.get(sender)
        if not profile:
            from models.user_profile import UserProfile
            profile = UserProfile(phone=sender, onboarding_step=None)
        _log(f"Profile loaded: {profile.name}")

        period = _detect_period()
        platform_list = [
            p for p in (profile.platforms or [])
            if p != "unknown" and _platform_transactions.get(p)
        ] or ["shopee"]
        _log(f"Period: {period} | Platforms: {platform_list}")
        _log(f"Cost totals in memory: { {p: list(c.keys()) for p, c in _cost_totals.items()} }")

        _log("Calling OrchestratorAgent...")
        result = OrchestratorAgent().run_sync(sender, period, platform_list, profile)
        _log(f"Orchestrator done. Status: {result.pipeline_status}")

        if result.combined_pnl:
            _last_pnl = result.combined_pnl
            _last_report_period = period
            _conversation.store_pnl(sender, result.combined_pnl)
            _log("P&L stored in memory.")
            from services.database import save_pnl
            save_pnl(sender, result.combined_pnl)
            _log("P&L saved to Supabase.")
        else:
            _log("WARNING: No combined_pnl returned from orchestrator.")

    except Exception as exc:
        import traceback
        print(f"  [Report] FAILED: {exc}")
        print(traceback.format_exc())
        _twiml_send(sender, f"❌ Report failed: {exc}\n\nCheck server logs for details.")
        _twiml_send(sender, f"❌ Report failed: {exc}")


def _handle_file_background(sender: str, media_url: str, filename: str) -> None:
    """Download, classify, parse and store a file sent via WhatsApp."""
    global _platform_transactions
    try:
        import httpx
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        auth_token  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
        resp.raise_for_status()
        content = resp.content

        classified = classify_file(filename, content)
        platform  = classified.platform
        file_type = classified.file_type

        platform_labels = {
            "shopee": "Shopee", "lazada": "Lazada", "amazon": "Amazon",
            "shopify": "Shopify", "tiktok": "TikTok Shop", "generic": "Internal", "unknown": "Unknown",
        }
        type_icons = {
            "transactions": "🛒", "cogs": "📦", "ads": "📣", "warehouse": "🏭",
            "payroll": "👥", "packaging": "📫", "expense": "💸", "unknown": "❓",
        }
        plabel = platform_labels.get(platform, platform.title())
        ticon  = type_icons.get(file_type, "❓")

        inspected = _inspect_upload(filename, content, classified)
        if not inspected["ok"]:
            _pending_files[sender] = {
                "content": content,
                "filename": filename,
                "status": "needs_label",
            }
            reply = (
                "I received your file, but it does not match an accepted Fynn template yet.\n\n"
                + "\n".join(f"• {err}" for err in inspected["errors"])
                + "\n\n"
                + _pending_upload_options()
            )
            _twiml_send(sender, reply)
            return

        if classified.confidence < STRICT_CONFIDENCE_THRESHOLD:
            _pending_files[sender] = {
                "content": content,
                "filename": filename,
                "status": "needs_confirmation",
                "classified": classified,
                "inspected": inspected,
            }
            reply = (
                _classification_preview(classified, inspected)
                + "\n\nConfidence is below 90%, so I have not saved it yet.\n"
                + _pending_upload_options()
            )
            _twiml_send(sender, reply)
            return

        _save_inspected_upload(content, inspected)
        period = inspected["period"]
        transaction_count = inspected["transaction_count"]
        profile = _conversation.profiles.get(sender)

        if not profile or not profile.is_onboarding_complete():
            _twiml_send(
                sender,
                f"{ticon} File saved.\n"
                "Complete setup first so I know which marketplaces and files to reconcile.\n"
                f"Setup link: {_setup_link_for_sender(sender)}",
            )
            return

        if file_type == "transactions" and transaction_count:
            stale = (_last_report_period and _last_report_period == period)
            reply = (
                f"{ticon} Got it! *{plabel}* finance file received.\n"
                f"📅 Period: {period}\n"
                f"📊 {transaction_count} transactions loaded\n\n"
                + (f"Refreshing your report for *{period}* now. 🔄"
                   if stale else
                   f"I’ll reconcile it and refresh your P&L now. 🚀")
            )
        else:
            stale = (_last_report_period and _last_report_period == period and period != "unknown")
            reply = (
                f"{ticon} Got it! *{file_type.title()}* costs for *{period}* saved.\n"
                + (f"Refreshing your *{period}* report now. 🔄"
                   if stale else
                   f"I’ll factor this into your refreshed report. 📋")
        )

        _twiml_send(sender, reply)
        _auto_refresh_report(sender)

    except Exception as exc:
        print(f"  [Webhook] File handling failed: {exc}")
        _twiml_send(sender, f"❌ Couldn't read that file: {exc}\nMake sure it's a CSV exported from Shopee/Lazada.")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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
    form = await request.form()
    From = str(form.get("From", "")).strip()
    Body = str(form.get("Body", "")).strip()
    NumMedia = str(form.get("NumMedia", "0"))

    print(f"\n[Webhook] Incoming from {From}: {Body or '[file]'}")

    sender = From
    message = Body

    # ── Incoming file ──────────────────────────────────────────────────────────
    try:
        media_count = int(NumMedia or 0)
    except ValueError:
        media_count = 0
    if media_count > 0:
        queued = 0
        rejected = 0
        for idx in range(media_count):
            media_url = str(form.get(f"MediaUrl{idx}", "")).strip()
            content_type = str(form.get(f"MediaContentType{idx}", "")).strip()
            if not media_url:
                continue
            if "csv" in content_type or "spreadsheet" in content_type or "text/plain" in content_type:
                filename = media_url.split("/")[-1] + ".csv"
                background_tasks.add_task(_handle_file_background, sender, media_url, filename)
                queued += 1
            else:
                rejected += 1

        if queued:
            if rejected:
                return _twiml_response(
                    f"📂 Got {queued} CSV/spreadsheet file(s). Classifying them now.\n"
                    f"I skipped {rejected} unsupported attachment(s)."
                )
            return _twiml_response(
                f"📂 Got {queued} file(s)! Classifying them now... I'll let you know what I find."
            )

        return _twiml_response(
            "I can only read CSV files right now. "
            "Export your data as .csv from Shopee/Lazada and send it here."
        )

    # Get profile (creates new one if first contact)
    profile, is_new = _conversation.profiles.get_or_create(sender)

    def _settings_link() -> str:
        return _setup_link_for_sender(sender)

    # Any settings-related message (reset, change settings, change currency, etc.)
    if message.lower() in {"reset", "clear", "restart"} or _conversation.is_settings_trigger(message, profile):
        _conversation.clear_history(sender)
        link = _settings_link()
        reply = f"Sure! Your current preferences are pre-filled.\nSetup link: {link}"
        return _twiml_response(reply)

    # New user → send settings link plus a short getting-started guide
    if not profile.is_onboarding_complete():
        from services.conversation import QUICK_START_EN, QUICK_START_ZH
        link = _settings_link()
        quick_start = QUICK_START_ZH if profile.language == "zh" else QUICK_START_EN
        reply = (
            f"Hey! 👋 I'm *Fynn*, your AI bookkeeper.\n\n"
            f"I help e-commerce sellers organise bookkeeping, reconcile payouts, and generate P&L reports.\n\n"
            f"Setup link: {link}\n"
            f"Choose how Fynn should prepare your reports. It takes 30 seconds.\n\n"
            f"{quick_start}\n\n"
            f"Once setup is done, send your files here and I'll take it from there."
        )
        return _twiml_response(reply)

    # Simple greeting/help request → show quick start and settings link
    if _conversation.is_greeting(message, profile):
        from services.conversation import QUICK_START_EN, QUICK_START_ZH
        link = _settings_link()
        quick_start = QUICK_START_ZH if profile.language == "zh" else QUICK_START_EN
        reply = (
            f"Hey {profile.name}! 👋\n\n"
            f"{quick_start}\n\n"
            f"Need to update your preferences?\nSetup link: {link}"
        )
        return _twiml_response(reply)

    # Send guide message after settings saved (first time onboarding complete)
    guide = _conversation.pop_pending_guide(sender)
    if guide:
        background_tasks.add_task(_twiml_send, sender, guide)
        return _twiml_response("You're all set! Sending you a quick guide now... 📖")

    # Pending reconciliation confirmation — user can proceed despite missing support files
    if sender in _pending_reconciliations:
        label = message.strip().lower()
        pending = _pending_reconciliations[sender]

        if label in {"proceed", "continue", "yes", "reconcile", "reconcile anyway", "run anyway"}:
            if pending.get("missing_essential"):
                return _twiml_response(
                    "I still need the marketplace finance export before reconciliation can run.\n\n"
                    + _format_missing_files(pending["missing_essential"], pending.get("missing_support", []))
                )
            background_tasks.add_task(_auto_refresh_report, sender, True)
            return _twiml_response("Okay — reconciling with the files available now. I’ll send the refreshed report when it’s done.")

        if label in {"wait", "hold", "not yet", "cancel"}:
            _pending_reconciliations.pop(sender, None)
            return _twiml_response("No problem. I’ll wait for the missing files before refreshing the report.")

    # Pending file clarification — user is labelling an unknown file
    if sender in _pending_files:
        label = message.strip().lower()
        pending = _pending_files[sender]

        # Allow user to dismiss the file
        if label in {"cancel", "skip", "ignore", "nevermind", "never mind", "discard"}:
            _pending_files.pop(sender)
            return _twiml_response("No problem — file discarded. Send another file whenever you're ready. 👍")

        if label in {"confirm", "yes", "y", "correct"}:
            if pending.get("status") != "needs_confirmation":
                return _twiml_response("I still need you to tell me what this file is.\n\n" + _pending_upload_options())

            _save_inspected_upload(pending["content"], pending["inspected"])
            _pending_files.pop(sender)
            background_tasks.add_task(_auto_refresh_report, sender)
            return _twiml_response(
                _classification_preview(pending["classified"], pending["inspected"])
                + "\n\nConfirmed and saved. I’ll refresh your P&L now."
            )

        if label in FILE_TYPE_LABELS:
            raw = pending["content"]
            platform_key, file_type_key = FILE_TYPE_LABELS[label]
            headers = _read_csv_headers(raw)
            manual_classified = ClassifiedFile(
                filename=pending.get("filename", "upload.csv"),
                platform=platform_key,
                file_type=file_type_key,
                confidence=1.0,
                notes="Confirmed by user",
                headers=headers,
            )
            inspected = _inspect_upload(pending.get("filename", "upload.csv"), raw, manual_classified)
            if not inspected["ok"]:
                return _twiml_response(
                    "I still cannot safely save this file with that label.\n\n"
                    + "\n".join(f"• {err}" for err in inspected["errors"])
                    + "\n\n"
                    + _pending_upload_options()
                )

            type_icons = {
                "transactions": "🛒", "cogs": "📦", "ads": "📣", "warehouse": "🏭",
                "payroll": "👥", "packaging": "📫", "expense": "💸",
            }
            ticon = type_icons.get(file_type_key, "📄")
            if file_type_key == "transactions":
                reply = (
                    f"{ticon} Got it — saved as *{label.title()}* transactions "
                    f"({inspected['transaction_count']} rows). I’ll refresh your P&L now. 🚀"
                )
            else:
                reply = (
                    f"{ticon} Got it — saved as *{file_type_key}* "
                    f"for *{inspected['period']}*. I’ll include it in your refreshed report. 📋"
                )

            _save_inspected_upload(raw, inspected)
            _pending_files.pop(sender)
            background_tasks.add_task(_auto_refresh_report, sender)
            return _twiml_response(reply)

        # Unrecognised label — remind user of valid options rather than silently falling through
        return _twiml_response(
            f"I didn't recognise *{label}*. Please reply with one of:\n"
            f"• *shopee* • *lazada* • *cogs* • *ads*\n"
            f"• *warehouse* • *payroll* • *packaging* • *expense*\n\n"
            f"Or reply *cancel* to discard the file."
        )

    # Onboarded user — check for report trigger
    if _conversation.is_report_trigger(message, profile):
        background_tasks.add_task(_auto_refresh_report, sender)
        return _twiml_response("On it! Checking your required files before refreshing the report. ⏳")

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
