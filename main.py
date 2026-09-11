"""Fynn — settlement reconciliation for Southeast Asian marketplaces.

FastAPI entry point. Three surfaces over one engine:

  Digest page  /c/{token} — the review surface. Tokenised, no login. Exceptions
               open a drawer where the accountant can question one item and
               approve it; journals and the audit trail are on the same page.
  WhatsApp     /webhook/whatsapp — Twilio posts here. Settlement files come in,
               the digest link goes out. Reviewing happens on the page.
  HTTP         /cycle/*, /approve, /post, /audit — the same operations for
               scripts and for anything the page does not cover yet.

All three go through utils.formatter.Cycle, so the figure on the page, the
figure in the message and the figure in the JSON are one reconciliation — never
three code paths that can drift.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from pydantic import BaseModel

from agents.explainer import opening_message, reply
from agents.investigator import investigate
from agents.notifier import Notifier
from data.sample_settlements import REPORTED_PAYOUTS, prior_cycle_lines, sample_lines
from models.firm_profile import SUPPORTED_LEDGERS, SUPPORTED_PLATFORMS, FirmProfileStore
from models.transaction import Platform, Side
from services.classification import RuleStore
from services.conversation import COMMANDS_HELP, QUICK_START_EN, QUICK_START_ZH, parse_command
from services.csv_parser import SettlementParseError, parse_reported, parse_settlement_csv
from services.database import save_posted_entry, save_settlement_file
from services.ledger import get_adapter
from services.whatsapp import issue_link, resolve
from utils.formatter import Cycle

app = FastAPI(
    title="Fynn",
    description="Settlement reconciliation for Shopee, Lazada and TikTok Shop",
    version="0.3.0",
)

# ── State ─────────────────────────────────────────────────────────────────────
# Cycles and rule stores are per firm, keyed by the firm's WhatsApp number.
# Rules are written through to Supabase as they are learned; the open cycle is
# in-process, which is the documented limitation (see README, "Not built yet").

_profiles = FirmProfileStore()
_cycles: dict[str, Cycle] = {}
_stores: dict[str, RuleStore] = {}
_notifier = Notifier()

# Requests that arrive over HTTP with no firm attached share one workspace, so
# curl against a local server behaves like a single firm.
API_FIRM = "api"

STATIC = os.path.join(os.path.dirname(__file__), "static")

_twilio_daily_limit_exhausted: bool = False


def _store_for(firm_id: str) -> RuleStore:
    """The firm's rule set, loaded from storage on first use."""
    if firm_id not in _stores:
        _stores[firm_id] = RuleStore.for_firm(firm_id)
    return _stores[firm_id]


def _cycle_for(firm_id: str) -> Optional[Cycle]:
    return _cycles.get(firm_id)


def _require_cycle(firm_id: str) -> Cycle:
    cycle = _cycles.get(firm_id)
    if cycle is None:
        raise HTTPException(404, "No cycle loaded. POST /cycle/sample first.")
    return cycle


def _firm_from_token(token: str) -> str:
    """Resolve a digest link back to the firm it was issued for.

    The token is the only credential the page has, so an expired or unknown one
    is a hard 410 — never a silent fall-through to somebody else's cycle.
    """
    entry = resolve(token)
    if entry is None:
        raise HTTPException(410, "This link has expired. Ask Fynn for a fresh one.")
    return entry.get("firm_id") or API_FIRM


def _firm_from_request(token: str = "", firm: str = API_FIRM) -> str:
    """Firm for an API call: the token when the page sent one, else the query."""
    if token:
        return _firm_from_token(token)
    return _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM


# ── Phone / link helpers ──────────────────────────────────────────────────────

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
    return f"{_app_url()}/setup?phone={_public_phone_param(sender)}"


def _upload_link_for_sender(sender: str) -> str:
    return f"{_app_url()}/upload?phone={_public_phone_param(sender)}"


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.3.0", "firms": len(_cycles)}


@app.get("/debug")
async def debug() -> dict:
    """Live state snapshot — what each firm has open right now."""
    return {
        "firms": {
            firm_id: {
                "cycle": cycle.cycle,
                "lines": len(cycle.lines),
                "open_exceptions": len(cycle.open_exceptions()),
                "platforms": sorted(p.value for p in cycle._results),
            }
            for firm_id, cycle in _cycles.items()
        },
        "rules": {firm_id: len(store.rules) for firm_id, store in _stores.items()},
        "profiles": list(_profiles._profiles.keys()),
        "ledger_adapter": get_adapter().name,
    }


# ── Cycle ingestion ───────────────────────────────────────────────────────────

def _ingest(
    firm_id: str,
    raw: bytes,
    filename: str,
    reported: str = "",
    default_platform: Optional[Platform] = None,
) -> Cycle:
    """Parse one settlement file and fold it into the firm's open cycle.

    Raises SettlementParseError, which callers turn into a 400 or a WhatsApp
    reply. A file that cannot be read must not half-load.
    """
    profile = _profiles.get(firm_id)
    existing = _cycles.get(firm_id)

    # An adjustments file names no marketplace anywhere, but the orders it
    # refers to are already in the open cycle. Offering those lets it be
    # attributed instead of rejected.
    known_orders = {
        l.order_id: l.platform for l in existing.lines if l.order_id
    } if existing is not None else {}

    # A firm that settles on exactly one platform has already told us which.
    if default_platform is None and profile is not None and len(profile.platforms) == 1:
        default_platform = Platform(profile.platforms[0])

    parsed = parse_settlement_csv(
        raw, filename=filename, default_platform=default_platform,
        known_orders=known_orders,
    )
    payouts = dict(parsed.reported_payouts)
    payouts.update(parse_reported(reported))

    if existing is None or existing.cycle != parsed.cycle:
        cycle = Cycle(
            lines=parsed.lines,
            reported_payouts=payouts,
            store=_store_for(firm_id),
            cycle=parsed.cycle,
            firm=profile.firm if profile else "Your firm",
            firm_id=firm_id,
        )
        cycle.run()
        _cycles[firm_id] = cycle
    else:
        cycle = existing
        cycle.add_lines(parsed.lines, payouts)

    save_settlement_file(firm_id, filename, parsed.cycle, raw, len(parsed.lines))
    return cycle


@app.post("/cycle/sample")
def load_sample(firm: str = Query(API_FIRM)):
    """Load the built-in sample cycle covering the three known edge cases."""
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    profile = _profiles.get(firm_id)
    cycle = Cycle(
        lines=sample_lines(),
        reported_payouts=dict(REPORTED_PAYOUTS),
        store=_store_for(firm_id),
        prior_cycles=prior_cycle_lines(),
        firm=profile.firm if profile else "Your firm",
        firm_id=firm_id,
    )
    cycle.run()
    _cycles[firm_id] = cycle
    return cycle.digest()


@app.post("/cycle/upload")
async def upload(
    file: UploadFile = File(...),
    reported: str = Form(""),
    firm: str = Form(API_FIRM),
):
    """Upload a settlement CSV.

    Canonical columns: platform,cycle,order,label,amount,date — but the parser
    also accepts a platform's own header names and infers the platform from the
    filename. `reported` is a semicolon list, e.g. "Lazada=1038;Shopee=725";
    omit it when the file states the payout on its own row.
    """
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    raw = await file.read()
    try:
        cycle = _ingest(firm_id, raw, file.filename or "upload.csv", reported)
    except SettlementParseError as exc:
        raise HTTPException(400, str(exc))
    return cycle.digest()


@app.get("/digest")
def digest(firm: str = Query(API_FIRM)):
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    return _require_cycle(firm_id).digest()


@app.post("/investigate/{key:path}")
def investigate_exception(key: str, firm: str = Query(API_FIRM)):
    """Ask the investigator for a suggestion on one exception.

    Returns a suggestion with a confidence score. It is never applied
    automatically — an accountant approves via /approve.
    """
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    cycle = _require_cycle(firm_id)
    for result in cycle.run().values():
        for exc in result.exceptions:
            if exc.key == key:
                ev = investigate(exc)
                if ev is None:
                    return {"key": key, "evidence": None,
                            "note": "No suggestion available. Set ANTHROPIC_API_KEY to enable the investigator."}
                return {"key": key, "evidence": ev.model_dump(mode="json")}
    raise HTTPException(404, f"No open exception with key {key}")


class ApproveRequest(BaseModel):
    key: str
    account: str
    side: Side = Side.DEBIT
    actor: str
    save_rule: bool = True
    firm: str = API_FIRM


@app.post("/approve")
def approve(req: ApproveRequest):
    """Record an accountant's decision and re-reconcile."""
    firm_id = _canonical_whatsapp_phone(req.firm) if req.firm != API_FIRM else API_FIRM
    cycle = _require_cycle(firm_id)
    cycle.approve(req.key, req.account, req.side, req.actor, req.save_rule)
    return cycle.digest()


@app.post("/post")
def post_entries(firm: str = Query(API_FIRM)):
    """Send balanced journal entries to the configured ledger adapter."""
    return _post_cycle(_canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM)


@app.get("/rules")
def rules(firm: str = Query(API_FIRM)):
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    store = _store_for(firm_id)
    return {"count": len(store.rules), "rules": store.export()}


@app.get("/audit", response_class=PlainTextResponse)
def audit_csv(firm: str = Query(API_FIRM)):
    """Working paper export."""
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    return _require_cycle(firm_id).trail.to_csv()


@app.get("/whatsapp/summary")
def whatsapp_summary(firm: str = Query(API_FIRM)):
    firm_id = _canonical_whatsapp_phone(firm) if firm != API_FIRM else API_FIRM
    return {"message": _require_cycle(firm_id).whatsapp_summary()}


# ── Web setup page ────────────────────────────────────────────────────────────

def _settings_html(phone: str, profile=None, saved: bool = False) -> str:
    app_url = _app_url()

    firm = profile.firm if profile and profile.firm != "Your firm" else ""
    actor = profile.actor if profile else ""
    lang = profile.language if profile else "en"
    ledger = profile.ledger if profile else "dry-run"
    platforms = set(profile.platforms if profile else SUPPORTED_PLATFORMS)

    saved_banner = """
    <div class="banner-success">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="8" fill="#16a34a"/><path d="M4.5 8l2.5 2.5 4.5-4.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Settings saved — head back to WhatsApp, Fynn is ready.
    </div>""" if saved else ""

    def sel(val, match): return "selected" if val == match else ""
    def chk(val, s): return "checked" if val in s else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fynn — Setup</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f0f2f5;
      color: #0d1b2a;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 16px 64px;
    }}

    .nav {{
      width: 100%;
      max-width: 480px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 36px;
    }}
    .logo {{
      font-size: 1.25rem;
      font-weight: 800;
      color: #0d1b2a;
      letter-spacing: -.5px;
    }}
    .logo span {{ color: #2563eb; }}
    .nav-tag {{
      font-size: .75rem;
      font-weight: 600;
      color: #2563eb;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: 99px;
      padding: 3px 10px;
    }}

    .card {{
      background: #fff;
      border-radius: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 24px rgba(0,0,0,.07);
      padding: 36px 32px;
      width: 100%;
      max-width: 480px;
    }}

    .card-header {{ margin-bottom: 32px; }}
    .card-header h1 {{
      font-size: 1.6rem;
      font-weight: 800;
      color: #0d1b2a;
      letter-spacing: -.4px;
      margin-bottom: 6px;
    }}
    .card-header p {{
      font-size: .9rem;
      color: #6b7280;
      line-height: 1.5;
    }}

    .section {{
      border-top: 1px solid #f3f4f6;
      padding-top: 24px;
      margin-bottom: 24px;
    }}
    .section-label {{
      font-size: .7rem;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: #9ca3af;
      margin-bottom: 16px;
    }}

    .field {{ margin-bottom: 18px; }}
    .field:last-child {{ margin-bottom: 0; }}
    .field > label {{
      display: block;
      font-size: .82rem;
      font-weight: 600;
      color: #374151;
      margin-bottom: 7px;
    }}
    .field .hint {{
      font-size: .75rem;
      color: #9ca3af;
      margin-top: 6px;
      line-height: 1.45;
    }}

    input[type=text],
    select {{
      width: 100%;
      padding: 10px 13px;
      border: 1.5px solid #e5e7eb;
      border-radius: 10px;
      font-size: .95rem;
      color: #0d1b2a;
      background: #fafafa;
      outline: none;
      transition: border-color .15s, box-shadow .15s;
      appearance: none;
      -webkit-appearance: none;
    }}
    input[type=text]:focus,
    select:focus {{
      border-color: #2563eb;
      box-shadow: 0 0 0 3px rgba(37,99,235,.1);
      background: #fff;
    }}

    .field-row {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .field-row .field {{ margin-bottom: 0; }}

    .pill-group {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .pill-group input[type=checkbox] {{ display: none; }}
    .pill-group label {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 13px;
      border: 1.5px solid #e5e7eb;
      border-radius: 99px;
      font-size: .85rem;
      font-weight: 500;
      color: #374151;
      cursor: pointer;
      transition: border-color .15s, background .15s, color .15s;
      user-select: none;
    }}
    .pill-group input[type=checkbox]:checked + label {{
      border-color: #2563eb;
      background: #eff6ff;
      color: #1d4ed8;
      font-weight: 600;
    }}
    .pill-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: currentColor;
      opacity: .5;
    }}
    .pill-group input[type=checkbox]:checked + label .pill-dot {{
      opacity: 1;
    }}

    .banner-success {{
      display: flex;
      align-items: center;
      gap: 10px;
      background: #f0fdf4;
      border: 1px solid #86efac;
      color: #15803d;
      padding: 12px 16px;
      border-radius: 10px;
      margin-bottom: 28px;
      font-size: .88rem;
      font-weight: 600;
    }}

    .btn-submit {{
      display: block;
      width: 100%;
      padding: 13px;
      background: #0d1b2a;
      color: #fff;
      border: none;
      border-radius: 10px;
      font-size: .95rem;
      font-weight: 700;
      cursor: pointer;
      margin-top: 28px;
      transition: background .15s, transform .1s;
      letter-spacing: -.1px;
    }}
    .btn-submit:hover {{ background: #1e3a5f; }}
    .btn-submit:active {{ transform: scale(.99); }}

    .footer-note {{
      text-align: center;
      font-size: .78rem;
      color: #9ca3af;
      margin-top: 20px;
      line-height: 1.5;
    }}
    .footer-note a {{ color: #2563eb; text-decoration: none; }}
  </style>
</head>
<body>

<nav class="nav">
  <div class="logo">F<span>y</span>nn</div>
  <div class="nav-tag">Settlement reconciliation</div>
</nav>

<div class="card">
  <div class="card-header">
    <h1>Set up your firm</h1>
    <p>Fynn reconciles marketplace payouts and prepares the journals. You approve every exception before anything posts.</p>
  </div>

  {saved_banner}

  <form method="POST" action="{app_url}/setup/save">
    <input type="hidden" name="phone" value="{phone}"/>

    <div class="section">
      <div class="section-label">Firm</div>

      <div class="field">
        <label>Firm name</label>
        <input type="text" name="firm" value="{firm}" placeholder="e.g. Tan &amp; Partners" required/>
      </div>

      <div class="field-row">
        <div class="field">
          <label>Approvals signed by</label>
          <input type="text" name="actor" value="{actor}" placeholder="e.g. D. Tan" required/>
        </div>
        <div class="field">
          <label>Language</label>
          <select name="language">
            <option value="en" {sel(lang,"en")}>English</option>
            <option value="zh" {sel(lang,"zh")}>中文</option>
          </select>
        </div>
      </div>
      <p class="hint">Every approval is written to the audit trail with this name and a timestamp.</p>
    </div>

    <div class="section">
      <div class="section-label">Marketplaces</div>
      <div class="field">
        <label>Which platforms do your clients settle on?</label>
        <div class="pill-group">
          <input type="checkbox" name="platforms" value="Shopee" id="p_shopee" {chk("Shopee", platforms)}/>
          <label for="p_shopee"><span class="pill-dot"></span>Shopee</label>
          <input type="checkbox" name="platforms" value="Lazada" id="p_lazada" {chk("Lazada", platforms)}/>
          <label for="p_lazada"><span class="pill-dot"></span>Lazada</label>
          <input type="checkbox" name="platforms" value="TikTok Shop" id="p_tiktok" {chk("TikTok Shop", platforms)}/>
          <label for="p_tiktok"><span class="pill-dot"></span>TikTok Shop</label>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-label">Ledger</div>
      <div class="field">
        <label>Where should approved journals go?</label>
        <select name="ledger">
          <option value="dry-run" {sel(ledger,"dry-run")}>Dry run — prepare the entry, post nothing</option>
          <option value="xero" {sel(ledger,"xero")}>Xero — DRAFT manual journal</option>
        </select>
        <p class="hint">Xero posting needs OAuth credentials that are not wired up yet; dry run returns the exact payload that would be sent.</p>
      </div>
    </div>

    <button type="submit" class="btn-submit">Save settings →</button>
  </form>

  <p class="footer-note">
    Rules your firm approves are saved and reused across every client.<br/>
    <a href="{app_url}/upload?phone={_public_phone_param(phone)}">Upload settlement files →</a>
  </p>
</div>

</body>
</html>"""


@app.get("/setup", response_class=HTMLResponse)
async def settings_page(phone: str = Query(...)) -> HTMLResponse:
    """Render the setup form for a firm, pre-filled if a profile exists."""
    canonical_phone = _canonical_whatsapp_phone(phone)
    profile = _profiles.get(canonical_phone)
    return HTMLResponse(_settings_html(canonical_phone, profile))


@app.get("/settings", response_class=HTMLResponse)
async def legacy_settings_page(phone: str = Query(...)) -> HTMLResponse:
    """Keep existing settings links working."""
    return await settings_page(phone)


@app.post("/setup/save", response_class=HTMLResponse)
async def settings_save(
    phone: str = Form(...),
    firm: str = Form(...),
    actor: str = Form(""),
    language: str = Form("en"),
    platforms: list[str] = Form(SUPPORTED_PLATFORMS),
    ledger: str = Form("dry-run"),
) -> HTMLResponse:
    """Save the setup form and send a WhatsApp confirmation."""
    phone = _canonical_whatsapp_phone(phone)
    profile, _ = _profiles.get_or_create(phone)

    profile.firm = firm.strip() or "Your firm"
    profile.actor = actor.strip()
    profile.language = language if language in ("en", "zh") else "en"
    profile.platforms = [p for p in platforms if p in SUPPORTED_PLATFORMS] or list(SUPPORTED_PLATFORMS)
    profile.ledger = ledger if ledger in SUPPORTED_LEDGERS else "dry-run"
    profile.onboarding_step = None  # setup complete

    _profiles.save(profile)

    # An open cycle keeps its own copy of the firm name for the digest header.
    cycle = _cycle_for(phone)
    if cycle is not None:
        cycle.firm = profile.firm

    quick_start = QUICK_START_ZH if profile.language == "zh" else QUICK_START_EN
    _twiml_send(
        phone,
        f"You're set up. ✅\n\n{profile.to_summary()}\n\n{quick_start}",
    )
    return HTMLResponse(_settings_html(phone, profile, saved=True))


@app.post("/settings/save", response_class=HTMLResponse)
async def legacy_settings_save(
    phone: str = Form(...),
    firm: str = Form(...),
    actor: str = Form(""),
    language: str = Form("en"),
    platforms: list[str] = Form(SUPPORTED_PLATFORMS),
    ledger: str = Form("dry-run"),
) -> HTMLResponse:
    """Keep existing settings form submissions working."""
    return await settings_save(
        phone=phone, firm=firm, actor=actor,
        language=language, platforms=platforms, ledger=ledger,
    )


# ── Upload web page ───────────────────────────────────────────────────────────

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(phone: str = Query("")) -> HTMLResponse:
    """Drag-and-drop settlement upload."""
    import json as _json

    app_url = _app_url()
    canonical_phone = _canonical_whatsapp_phone(phone) if phone else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Fynn — Upload settlements</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
          background:#f9fafb;color:#111;min-height:100vh;padding:32px 16px}}
    .wrap{{max-width:640px;margin:0 auto}}
    .logo{{font-size:1.1rem;font-weight:800;color:#2563eb;margin-bottom:8px}}
    h1{{font-size:1.5rem;font-weight:700;margin-bottom:4px}}
    .sub{{color:#6b7280;font-size:.9rem;margin-bottom:28px;line-height:1.55}}
    .dropzone{{border:2px dashed #d1d5db;border-radius:16px;padding:40px;
               text-align:center;background:#fff;cursor:pointer;transition:all .2s}}
    .dropzone.drag{{border-color:#2563eb;background:#eff6ff}}
    .dropzone-icon{{font-size:2.5rem;margin-bottom:12px}}
    .dropzone p{{color:#6b7280;font-size:.95rem}}
    .dropzone strong{{color:#2563eb;cursor:pointer}}
    #fileInput{{display:none}}
    .field{{margin-top:18px}}
    .field label{{display:block;font-size:.8rem;font-weight:600;color:#374151;margin-bottom:6px}}
    .field input{{width:100%;padding:10px 13px;border:1.5px solid #e5e7eb;border-radius:10px;
                  font-size:.92rem;background:#fff;outline:none}}
    .field .hint{{font-size:.75rem;color:#9ca3af;margin-top:6px}}
    .file-list{{margin-top:20px;display:flex;flex-direction:column;gap:10px}}
    .file-card{{background:#fff;border-radius:12px;padding:14px 16px;
                box-shadow:0 1px 4px rgba(0,0,0,.08);display:flex;
                align-items:center;gap:12px}}
    .file-card .icon{{font-size:1.4rem}}
    .file-card .info{{flex:1;min-width:0}}
    .file-card .name{{font-weight:600;font-size:.9rem;white-space:nowrap;
                      overflow:hidden;text-overflow:ellipsis}}
    .file-card .meta{{font-size:.78rem;color:#6b7280;margin-top:2px;line-height:1.5}}
    .badge{{display:inline-block;padding:2px 8px;border-radius:999px;
            font-size:.72rem;font-weight:700;color:#fff;margin-right:4px;background:#374151}}
    .status-pending{{color:#9ca3af}}
    .status-ok{{color:#059669}}
    .status-warn{{color:#b45309}}
    .status-err{{color:#dc2626}}
    .btn{{display:block;width:100%;padding:13px;background:#0d1b2a;color:#fff;
          border:none;border-radius:10px;font-size:1rem;font-weight:600;
          cursor:pointer;margin-top:20px;transition:background .2s}}
    .btn:hover{{background:#1e3a5f}}
    .btn:disabled{{background:#cbd5e1;cursor:not-allowed}}
    .result{{margin-top:20px;padding:16px;border-radius:12px;font-size:.9rem;
             background:#ecfdf5;border:1px solid #6ee7b7;color:#065f46;display:none;line-height:1.6}}
    .result.warn{{background:#fffbeb;border-color:#fcd34d;color:#92400e}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="logo">Fynn</div>
  <h1>Upload settlement files</h1>
  <p class="sub">Drop the settlement export from Shopee, Lazada or TikTok Shop.
  Fynn classifies each line against your firm's rules and checks the total against the payout the platform reported.<br>
  Anything that does not tie out comes back as an exception for you to approve.</p>

  <div class="dropzone" id="dropzone">
    <div class="dropzone-icon">📂</div>
    <p>Drag &amp; drop settlement CSVs here<br>or <strong onclick="document.getElementById('fileInput').click()">browse files</strong></p>
  </div>
  <input type="file" id="fileInput" multiple accept=".csv"/>

  <div class="field">
    <label>Reported payout (optional)</label>
    <input type="text" id="reported" placeholder="Lazada=1038.00;Shopee=725.00"/>
    <p class="hint">Only needed when the file does not state the payout on its own row.</p>
  </div>

  <div class="file-list" id="fileList"></div>
  <div class="result" id="result"></div>
  <button class="btn" id="uploadBtn" disabled onclick="uploadAll()">Reconcile →</button>
</div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileList  = document.getElementById('fileList');
const uploadBtn = document.getElementById('uploadBtn');
const result    = document.getElementById('result');
const phone     = {_json.dumps(canonical_phone)};
let selectedFiles = [];

dropzone.addEventListener('dragover', e => {{ e.preventDefault(); dropzone.classList.add('drag'); }});
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', e => {{
  e.preventDefault(); dropzone.classList.remove('drag');
  addFiles([...e.dataTransfer.files]);
}});
fileInput.addEventListener('change', () => addFiles([...fileInput.files]));

function addFiles(files) {{
  files.filter(f => f.name.toLowerCase().endsWith('.csv')).forEach(f => {{
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
        <div class="meta status-pending" id="meta-${{i}}">Waiting to reconcile...</div>
      </div>
    </div>`).join('');
  uploadBtn.disabled = selectedFiles.length === 0;
}}

async function uploadAll() {{
  uploadBtn.disabled = true;
  result.style.display = 'none';
  let latest = null;

  // Sequential, not parallel: each file folds into the same open cycle, and
  // concurrent writes to it would race.
  for (let i = 0; i < selectedFiles.length; i++) {{
    const f = selectedFiles[i];
    const meta = document.getElementById('meta-' + i);
    meta.className = 'meta status-pending';
    meta.textContent = 'Reconciling...';

    const fd = new FormData();
    fd.append('file', f);
    fd.append('reported', document.getElementById('reported').value || '');
    if (phone) fd.append('phone', phone);

    try {{
      const res = await fetch('{app_url}/upload/classify', {{method:'POST', body:fd}});
      const data = await res.json();
      if (res.ok) {{
        latest = data;
        const openCount = data.open_exceptions;
        meta.className = 'meta ' + (openCount ? 'status-warn' : 'status-ok');
        meta.innerHTML = `
          <span class="badge">${{data.cycle}}</span>
          ${{data.platforms.join(', ')}} · ${{data.lines_added}} lines
          · ${{openCount ? openCount + ' exception(s) to review' : 'ties out'}}`;
      }} else {{
        meta.className = 'meta status-err';
        meta.textContent = 'Error: ' + (data.detail || data.error || 'unknown');
      }}
    }} catch(e) {{
      meta.className = 'meta status-err';
      meta.textContent = 'Upload failed: ' + e.message;
    }}
  }}

  uploadBtn.disabled = false;
  if (latest) {{
    result.style.display = 'block';
    result.className = latest.open_exceptions ? 'result warn' : 'result';
    result.innerHTML = latest.open_exceptions
      ? `⚠️ Cycle ${{latest.cycle}}: ${{latest.lines_classified}} of ${{latest.lines_total}} lines classified.
         <strong>${{latest.open_exceptions}} need your decision.</strong><br>
         Reply on WhatsApp with <em>digest</em> to review them, or use POST /approve.`
      : `✅ Cycle ${{latest.cycle}}: all ${{latest.lines_total}} lines classified and every platform ties out.
         Reply <em>post</em> on WhatsApp to send the journals.`;
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
    reported: str = Form(""),
    phone: str = Form(""),
) -> JSONResponse:
    """Reconcile one uploaded settlement CSV into the firm's open cycle."""
    filename = file.filename or "upload.csv"
    if not filename.lower().endswith(".csv"):
        return JSONResponse(content={"error": "Only .csv files are supported."}, status_code=400)

    firm_id = _canonical_whatsapp_phone(phone) if phone else API_FIRM
    raw = await file.read()

    try:
        cycle = _ingest(firm_id, raw, filename, reported)
    except SettlementParseError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=422)

    d = cycle.digest()
    print(f"\n[Upload] {filename} → cycle {d['cycle']}, {d['open_exceptions']} open exception(s)")

    # The web page is a second door into the same cycle; tell the accountant on
    # WhatsApp so the two surfaces do not drift out of sync.
    if phone:
        background_tasks.add_task(_notifier.digest, firm_id, cycle)

    return JSONResponse(content={
        "filename": filename,
        "cycle": d["cycle"],
        "platforms": sorted(p["platform"] for p in d["platforms"]),
        "lines_added": len(cycle.lines),
        "lines_total": d["lines_total"],
        "lines_classified": d["lines_classified"],
        "open_exceptions": d["open_exceptions"],
        "exceptions": d["exceptions"],
    })


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def _is_supported_upload_media(content_type: str, media_url: str) -> bool:
    """Return True for media Twilio may use for CSV-style document uploads."""
    ct = (content_type or "").strip().lower()
    url = (media_url or "").strip().lower()
    accepted_tokens = (
        "csv",
        "spreadsheet",
        "text/plain",
        "text/comma-separated-values",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats",
        "application/octet-stream",
    )
    if any(token in ct for token in accepted_tokens):
        return True
    return url.endswith(".csv") or url.endswith(".xlsx")


def _media_filename(body: str, media_url: str, content_type: str, index: int) -> str:
    """Work out what the attachment was actually called.

    Twilio's media URL ends in an opaque SID — ME524fbc08… — which names no
    platform and no period, so deriving the filename from it throws away the
    two things the parser most wants. WhatsApp sends a document's real filename
    as the message body, so that is used when it looks like one; only the first
    attachment can claim it.
    """
    candidate = (body or "").strip()
    if index == 0 and candidate and "\n" not in candidate:
        if candidate.lower().endswith((".csv", ".xlsx", ".xls", ".xlsm")):
            # Strip any path the sender's client prepended.
            return os.path.basename(candidate)

    suffix = ".xlsx" if "spreadsheet" in (content_type or "").lower() else ".csv"
    return media_url.rstrip("/").split("/")[-1] + suffix


def _handle_file_background(sender: str, media_url: str, filename: str) -> None:
    """Download a settlement file from Twilio, reconcile it, send the digest."""
    try:
        import httpx
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        resp = httpx.get(
            media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True
        )
        resp.raise_for_status()
        content = resp.content
    except Exception as exc:
        print(f"  [Webhook] Media download failed: {exc}")
        _twiml_send(sender, f"❌ I couldn't download that file: {exc}")
        return

    try:
        cycle = _ingest(sender, content, filename)
    except SettlementParseError as exc:
        _twiml_send(
            sender,
            f"❌ I couldn't read that as a settlement file.\n\n{exc}\n\n"
            "Expected columns: platform, cycle, order, label, amount, date — "
            "or a marketplace's own export headers.",
        )
        return
    except Exception as exc:
        print(f"  [Webhook] File handling failed: {exc}")
        _twiml_send(sender, f"❌ Something went wrong reconciling that file: {exc}")
        return

    _notifier.digest(sender, cycle, base_url=_app_url())


def _digest_link_reply(sender: str, cycle: Cycle, lead: str) -> str:
    """A one-line summary plus a fresh link to the digest page."""
    link = issue_link(cycle.cycle, firm_id=sender, base_url=_app_url())
    cycle.trail.add("source", f"Digest link sent to {sender}")
    return (
        f"{lead}\n\n{cycle.whatsapp_summary()}\n\n"
        f"Open digest: {link['url']}\n\nNo login needed."
    )


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Twilio webhook — receives incoming WhatsApp messages and replies as Fynn.

    Twilio POSTs form data here whenever an accountant sends a message.
    This endpoint must be publicly accessible (use ngrok for local dev).

    Routing:
      - an attached CSV → reconciled in the background, link sent when done
      - "digest" → a fresh link to the current cycle
      - "setup" → the firm settings link
      - anything else → the link, which is nearly always what was wanted

    Reviewing and approving happen on the page, not in the thread. An approval
    is recorded against a named person, and it should be made with the evidence,
    the amounts and the resulting journal all visible at once.
    """
    form = await request.form()
    From = str(form.get("From", "")).strip()
    Body = str(form.get("Body", "")).strip()
    NumMedia = str(form.get("NumMedia", "0"))

    print(f"\n[Webhook] Incoming from {From}: {Body or '[file]'}")

    sender = From
    message = Body

    # ── Incoming file ─────────────────────────────────────────────────────────
    try:
        media_count = int(NumMedia or 0)
    except ValueError:
        media_count = 0

    if media_count > 0:
        queued, rejected = 0, 0
        for idx in range(media_count):
            media_url = str(form.get(f"MediaUrl{idx}", "")).strip()
            content_type = str(form.get(f"MediaContentType{idx}", "")).strip()
            if not media_url:
                continue
            print(f"  [Webhook] Media {idx}: content_type={content_type or 'unknown'} url={media_url}")
            if _is_supported_upload_media(content_type, media_url):
                filename = _media_filename(message, media_url, content_type, idx)
                background_tasks.add_task(_handle_file_background, sender, media_url, filename)
                queued += 1
            else:
                rejected += 1

        if queued:
            note = f" I skipped {rejected} unsupported attachment(s)." if rejected else ""
            return _twiml_response(
                f"📂 Got {queued} settlement file(s). Reconciling now — "
                f"I'll send the cycle summary in a moment.{note}"
            )

        return _twiml_response(
            f"I received {media_count} attachment(s), but none looked like CSV files. "
            "Export the settlement report as .csv and send it here."
        )

    profile, _is_new = _profiles.get_or_create(sender)
    command = parse_command(message)

    # Settings, and the reset path, always work.
    if command.kind == "setup":
        return _twiml_response(
            "Your current settings are pre-filled.\n"
            f"Setup link: {_setup_link_for_sender(sender)}"
        )

    if not profile.is_onboarding_complete():
        quick_start = QUICK_START_ZH if profile.language == "zh" else QUICK_START_EN
        return _twiml_response(
            "Hey 👋 I'm *Fynn*. I reconcile Shopee, Lazada and TikTok Shop settlements "
            "and prepare the journals — for your firm, across every client.\n\n"
            f"Setup link: {_setup_link_for_sender(sender)}\n"
            "It takes 30 seconds and tells me who signs off on decisions.\n\n"
            f"{quick_start}"
        )

    cycle = _cycle_for(sender)

    if command.kind == "greeting":
        quick_start = QUICK_START_ZH if profile.language == "zh" else QUICK_START_EN
        return _twiml_response(
            f"Hey {profile.approver()} 👋\n\n{quick_start}\n\n{COMMANDS_HELP}"
        )

    if cycle is None:
        return _twiml_response(
            "No cycle open yet. Send me a settlement CSV and I'll reconcile it.\n\n"
            f"You can also drop files here: {_upload_link_for_sender(sender)}"
        )

    if command.kind == "digest":
        return _twiml_response(_digest_link_reply(sender, cycle, "Here's your cycle."))

    # Everything else: the sender almost certainly wants the digest. Say what
    # else is available rather than refusing to answer.
    return _twiml_response(
        _digest_link_reply(
            sender,
            cycle,
            "Reviewing and approving happen on the digest page — it has the "
            "evidence and the journals next to each figure.",
        )
        + f"\n\n{COMMANDS_HELP}"
    )


def _twiml_response(message: str) -> Response:
    """Wrap a reply string in TwiML XML so Twilio sends it as a WhatsApp message."""
    # Escape XML special characters
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{safe}</Message>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


def _twiml_send(to: str, message: str) -> None:
    """Send an out-of-band WhatsApp message via the Twilio REST API.

    Used for anything that happens outside a webhook reply — a background
    reconciliation finishing, or the setup form confirming.
    """
    global _twilio_daily_limit_exhausted
    if _twilio_daily_limit_exhausted:
        print("  [Webhook] Twilio daily limit already exhausted — skipping out-of-band send.")
        return

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
        if "exceeded the 50 daily messages limit" in str(exc) or "HTTP 429" in str(exc):
            _twilio_daily_limit_exhausted = True
        print(f"  [Webhook] Out-of-band send failed: {exc}")


# ── Digest page + its API ─────────────────────────────────────────────────────
# The page is the review surface. It is reached by a tokenised link with no
# login, so every endpoint below resolves the firm from that token — see
# _firm_from_token. The page never names a firm itself.


class ChatRequest(BaseModel):
    key: str
    history: list[dict]


class PageApproveRequest(BaseModel):
    key: str
    account: str
    side: Side = Side.DEBIT
    save_rule: bool = True


class NotifyRequest(BaseModel):
    to: str
    base_url: Optional[str] = None


def _find_exception(cycle: Cycle, key: str):
    for result in cycle.run().values():
        for exc in result.exceptions:
            if exc.key == key:
                return exc
    raise HTTPException(404, f"No open exception with key {key}")


def _post_cycle(firm_id: str) -> dict:
    """Post every platform's journal for one firm. Refuses while anything is open."""
    cycle = _require_cycle(firm_id)
    profile = _profiles.get(firm_id)

    results = cycle.run()
    blocked = [p.value for p, r in results.items() if not r.ties_out]
    if blocked:
        raise HTTPException(
            409, f"Unresolved exceptions on: {', '.join(blocked)}. Resolve before posting."
        )

    adapter = get_adapter(profile.ledger if profile else None)
    actor = profile.approver() if profile else "API caller"
    out = []
    for _platform, result in results.items():
        try:
            out.append(adapter.post(result.journal))
        except NotImplementedError as exc:
            # The Xero adapter prepares the payload but cannot send it yet. That
            # is a configuration state, not a server fault.
            raise HTTPException(501, str(exc).split(". Payload prepared")[0])
        cycle.trail.add("post", f"{result.journal.reference} sent to {adapter.name}", actor=actor)
        save_posted_entry(
            firm_id, cycle.cycle, result.journal, adapter.name, actor, cycle.trail.to_csv()
        )
    return {"adapter": adapter.name, "entries": out}


@app.get("/", include_in_schema=False)
@app.get("/c/{token}", include_in_schema=False)
def digest_page(token: str = ""):
    """The month-end digest. Tokenised, no login."""
    if token:
        _firm_from_token(token)  # 410s on an expired or unknown link
    return FileResponse(os.path.join(STATIC, "digest.html"))


@app.get("/api/digest")
def api_digest(token: str = Query(""), firm: str = Query(API_FIRM)):
    return _require_cycle(_firm_from_request(token, firm)).digest()


@app.get("/api/exception/opening")
def api_opening(key: str, token: str = Query(""), firm: str = Query(API_FIRM)):
    """First message in the drawer. Deterministic — no model call."""
    cycle = _require_cycle(_firm_from_request(token, firm))
    exc = _find_exception(cycle, key)
    # Ask the investigator only where the deterministic layer found nothing. It
    # is a suggestion either way, never a decision.
    if exc.evidence is None:
        exc.evidence = investigate(exc)
    return {"key": key, "message": opening_message(exc)}


@app.post("/api/exception/chat")
def api_chat(req: ChatRequest, token: str = Query(""), firm: str = Query(API_FIRM)):
    """Follow-up questions about one exception. This is the only LLM surface
    the accountant talks to, and it cannot approve anything."""
    cycle = _require_cycle(_firm_from_request(token, firm))
    exc = _find_exception(cycle, req.key)
    return {"message": reply(exc, req.history, cycle.lines, cycle.prior)}


@app.post("/api/approve")
def api_approve(req: PageApproveRequest, token: str = Query(""), firm: str = Query(API_FIRM)):
    """Approve from the page.

    The actor is taken from the firm's settings, not from the request body — the
    audit trail should name whoever the firm said signs off, and a page with no
    login cannot be trusted to say who is holding the phone.
    """
    firm_id = _firm_from_request(token, firm)
    cycle = _require_cycle(firm_id)
    profile = _profiles.get(firm_id)
    actor = profile.approver() if profile else "Unnamed approver"
    cycle.approve(req.key, req.account, req.side, actor, req.save_rule)
    return cycle.digest()


@app.post("/api/post")
def api_post(token: str = Query(""), firm: str = Query(API_FIRM)):
    return _post_cycle(_firm_from_request(token, firm))


@app.get("/api/journal/{platform}")
def api_journal(platform: str, token: str = Query(""), firm: str = Query(API_FIRM)):
    cycle = _require_cycle(_firm_from_request(token, firm))
    for p, result in cycle.run().items():
        if p.value == platform and result.journal:
            j = result.journal
            return {
                "reference": j.reference,
                "lines": [l.model_dump(mode="json") for l in j.lines],
                "total_debit": j.total_debit,
                "total_credit": j.total_credit,
                "balanced": j.balanced,
            }
    raise HTTPException(404, f"No journal for {platform}")


@app.get("/api/audit.json")
def api_audit_json(token: str = Query(""), firm: str = Query(API_FIRM)):
    return {"records": _require_cycle(_firm_from_request(token, firm)).trail.to_dicts()}


@app.get("/api/audit", response_class=PlainTextResponse)
def api_audit_csv(token: str = Query(""), firm: str = Query(API_FIRM)):
    return _require_cycle(_firm_from_request(token, firm)).trail.to_csv()


@app.post("/notify")
def notify(req: NotifyRequest):
    """Issue a digest link and send it over WhatsApp."""
    to = _canonical_whatsapp_phone(req.to)
    cycle = _require_cycle(to)
    result = _notifier.digest(to, cycle, base_url=req.base_url or _app_url())
    return {"delivery": result}
