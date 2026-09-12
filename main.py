"""Fynn — settlement reconciliation for Southeast Asian marketplaces.

FastAPI entry point. One workspace, one web application:

  /            the app — upload, review, approve, post, settings
  /api/*       everything the app calls
  /cycle/*, /approve, /post, /audit
               the same operations for scripts

The engine is utils.formatter.Cycle. The page and the JSON are the same
reconciliation rendered twice, never two code paths that can drift.

A deployment is one firm's workspace behind one shared password — see the
Access section below. There is no per-user state: every store already takes a
firm id, so supporting several firms, or knowing which person approved
something, is an authentication problem rather than an engine one.
"""

import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from agents.explainer import opening_message, reply
from agents.investigator import investigate
from data.sample_settlements import REPORTED_PAYOUTS, prior_cycle_lines, sample_lines
from models.account_map import AccountMap, suggest
from models.firm_profile import (
    SUPPORTED_LEDGERS,
    SUPPORTED_PLATFORMS,
    WORKSPACE_ID,
    FirmProfileStore,
)
from models.transaction import Platform, Side
from services.classification import RuleStore
from services.csv_parser import SettlementParseError, parse_reported, parse_settlement_csv
from services import connections, oauth
from services.database import diagnose, save_posted_entry, save_settlement_file
from services.ledger import fetch_chart, get_adapter
from utils.formatter import Cycle

app = FastAPI(
    title="Fynn",
    description="Settlement reconciliation for Shopee, Lazada and TikTok Shop",
    version="0.4.0",
)

STATIC = os.path.join(os.path.dirname(__file__), "static")

# ── Access ────────────────────────────────────────────────────────────────────
# A shared password over HTTP Basic. Not an accounts system: every visitor is
# the same workspace, and the audit trail names whoever the firm put in
# Settings, not whoever typed the password. It exists so that a public URL is
# not an open door to approving and posting journals.
#
# With FYNN_PASSWORD unset, local requests are allowed and everything else is
# refused. Deploying without setting it therefore produces a locked app with an
# explanatory message, rather than an open one — the failure that matters here
# is the silent one.

AUTH_USER = os.getenv("FYNN_USER", "fynn").strip() or "fynn"
AUTH_PASSWORD = os.getenv("FYNN_PASSWORD", "").strip()
OPEN_PATHS = {"/health", "/login", "/logout"}
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

SESSION_COOKIE = "fynn_session"
SESSION_DAYS = 14


def _sign(expires: int) -> str:
    """A session token: when it lapses, and proof we issued it.

    Keyed on the password itself, so changing FYNN_PASSWORD invalidates every
    session already handed out. That is the only revocation a shared password
    can offer, and it should not need a second secret to work.
    """
    signature = hmac.new(
        AUTH_PASSWORD.encode(), str(expires).encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires}.{signature}"


def _valid_session(token: str) -> bool:
    expires, _, signature = (token or "").partition(".")
    if not expires.isdigit() or not signature:
        return False
    if int(expires) < int(time.time()):
        return False
    expected = hmac.new(
        AUTH_PASSWORD.encode(), expires.encode(), hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(signature, expected)


def _authorised(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except Exception:
        return False
    # Compare both halves in constant time, and always both, so the response
    # time does not reveal whether the username was right.
    return secrets.compare_digest(user, AUTH_USER) & secrets.compare_digest(
        password, AUTH_PASSWORD
    )


@app.middleware("http")
async def require_password(request: Request, call_next):
    if request.url.path in OPEN_PATHS:
        return await call_next(request)

    if not AUTH_PASSWORD:
        client = request.client.host if request.client else ""
        if client in LOCAL_HOSTS:
            return await call_next(request)
        return PlainTextResponse(
            "Fynn has no password set, so it will not serve anything beyond this "
            "machine.\n\nSet FYNN_PASSWORD in the environment and restart.\n",
            status_code=503,
        )

    if _valid_session(request.cookies.get(SESSION_COOKIE, "")) or _authorised(request):
        return await call_next(request)

    # A browser gets the sign-in page; anything else gets the Basic challenge,
    # so curl and scripts keep working with -u.
    if "text/html" in request.headers.get("accept", ""):
        wanted = request.url.path
        if request.url.query:
            wanted += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(wanted, safe='')}", status_code=303)

    return Response(
        status_code=401,
        content="Authentication required.",
        headers={"WWW-Authenticate": 'Basic realm="Fynn", charset="UTF-8"'},
    )


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"))


@app.post("/login", include_in_schema=False)
def login(password: str = Form(...), next: str = Form("/")):
    if not AUTH_PASSWORD or not secrets.compare_digest(password, AUTH_PASSWORD):
        return RedirectResponse("/login?error=1", status_code=303)

    # Only ever redirect within this site.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    expires = int(time.time()) + SESSION_DAYS * 86400
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, _sign(expires), max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        # Railway terminates TLS, so the cookie travels over https there. Left
        # off for local http, where it would simply never be sent back.
        secure=bool(os.getenv("RAILWAY_PUBLIC_DOMAIN")),
    )
    return response


@app.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response

# ── State ─────────────────────────────────────────────────────────────────────
# One workspace. Rules are written through to Supabase as they are learned; the
# open cycle is in-process, which is the documented limitation (see README).

_profiles = FirmProfileStore()
_cycle: Optional[Cycle] = None
_store: Optional[RuleStore] = None
# Keyed by ledger: a Xero code and a QuickBooks id are different things, so a
# firm that switches ledgers maps again rather than inheriting the wrong codes.
_account_maps: dict[str, AccountMap] = {}


def _rules() -> RuleStore:
    """The firm's rule set, loaded from storage on first use."""
    global _store
    if _store is None:
        _store = RuleStore.for_firm(WORKSPACE_ID)
    return _store


def base_url() -> str:
    """The app's own public URL, which the OAuth redirect must match exactly."""
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else "http://localhost:8000"


def _accounts(ledger: Optional[str] = None) -> AccountMap:
    """The firm's chart-of-accounts mapping for a ledger, loaded on first use."""
    ledger = (ledger or _profile().ledger or "dry-run").lower()
    if ledger not in _account_maps:
        _account_maps[ledger] = AccountMap.for_firm(WORKSPACE_ID, ledger)
    return _account_maps[ledger]


def accounts_in_use() -> list[str]:
    """Every account name a posting would touch.

    Taken from the rules the firm has accumulated and from the open cycle's
    journals, so the mapping screen is useful before a cycle exists as well as
    during one. Clearing accounts are generated per platform rather than named
    in any rule, so they are added from settings.
    """
    names = {r.account for r in _rules().rules}
    for platform in _profile().platforms:
        names.add(f"{platform} Clearing Account")
    if _cycle is not None:
        for result in _cycle.run().values():
            if result.journal:
                names.update(l.account for l in result.journal.lines)
    return sorted(names)


def _require_cycle() -> Cycle:
    if _cycle is None:
        raise HTTPException(404, "No cycle open. Upload a settlement file first.")
    return _cycle


def _profile():
    profile, _ = _profiles.get_or_create()
    return profile


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.4.0", "cycle_open": _cycle is not None}


@app.get("/debug")
async def debug() -> dict:
    """Live state snapshot."""
    return {
        "cycle": {
            "period": _cycle.cycle,
            "lines": len(_cycle.lines),
            "open_exceptions": len(_cycle.open_exceptions()),
            "platforms": sorted(p.value for p in _cycle._results),
        } if _cycle else None,
        "rules": len(_rules().rules),
        "firm": _profile().firm,
        "ledger_adapter": get_adapter().name,
    }


# ── Ingestion ─────────────────────────────────────────────────────────────────

def _ingest(raw: bytes, filename: str, reported: str = "") -> Cycle:
    """Parse one settlement file and fold it into the open cycle.

    Raises SettlementParseError, which callers turn into a 400. A file that
    cannot be read must not half-load.
    """
    global _cycle
    profile = _profile()

    # An adjustments file names no marketplace anywhere, but the orders it
    # refers to are already in the open cycle. Offering those lets it be
    # attributed instead of rejected.
    known_orders = {
        l.order_id: l.platform for l in _cycle.lines if l.order_id
    } if _cycle is not None else {}

    # A firm that settles on exactly one platform has already told us which.
    default_platform = (
        Platform(profile.platforms[0]) if len(profile.platforms) == 1 else None
    )

    parsed = parse_settlement_csv(
        raw, filename=filename, default_platform=default_platform,
        known_orders=known_orders,
    )
    payouts = dict(parsed.reported_payouts)
    payouts.update(parse_reported(reported))

    if _cycle is None or _cycle.cycle != parsed.cycle:
        _cycle = Cycle(
            lines=parsed.lines, reported_payouts=payouts, store=_rules(),
            cycle=parsed.cycle, firm=profile.firm, firm_id=WORKSPACE_ID,
        )
        _cycle.run()
    else:
        _cycle.add_lines(parsed.lines, payouts)

    save_settlement_file(WORKSPACE_ID, filename, parsed.cycle, raw, len(parsed.lines))
    return _cycle


def _post_cycle() -> dict:
    """Post every platform's journal. Refuses while anything is open."""
    cycle = _require_cycle()
    profile = _profile()

    results = cycle.run()
    blocked = [p.value for p, r in results.items() if not r.ties_out]
    if blocked:
        raise HTTPException(
            409, f"Unresolved exceptions on: {', '.join(blocked)}. Resolve before posting."
        )

    adapter = get_adapter(
        profile.ledger, _accounts(profile.ledger),
        connections.get(WORKSPACE_ID, profile.ledger),
    )
    actor = profile.approver()
    out = []
    for _platform, result in results.items():
        try:
            out.append(adapter.post(result.journal))
        except NotImplementedError as exc:
            # The live adapters prepare the payload but cannot send it yet. That
            # is a configuration state, not a server fault.
            raise HTTPException(501, str(exc).split(". Payload prepared")[0])
        except RuntimeError as exc:
            # Unmapped accounts, or missing credentials. Both are things the
            # firm can fix, and the message says which.
            raise HTTPException(409, str(exc))
        cycle.trail.add("post", f"{result.journal.reference} sent to {adapter.name}", actor=actor)
        save_posted_entry(
            WORKSPACE_ID, cycle.cycle, result.journal, adapter.name, actor, cycle.trail.to_csv()
        )
    return {"adapter": adapter.name, "entries": out}


# ── The app ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "app.html"))


class ApproveRequest(BaseModel):
    key: str
    account: str
    # Optional: the direction follows the amount's sign, so a caller only has to
    # name the account. Supplying a side records intent; it never overrides the
    # sign, because the entry has to balance.
    side: Optional[Side] = None
    save_rule: bool = True
    # "label" saves the decision against this fee name; "category" widens it to
    # the platform's own classification, covering fee names not yet seen.
    scope: str = "label"
    actor: Optional[str] = None


class ChatRequest(BaseModel):
    key: str
    history: list[dict]


class SettingsRequest(BaseModel):
    firm: str
    actor: str = ""
    platforms: list[str] = SUPPORTED_PLATFORMS
    ledger: str = "dry-run"


@app.get("/api/state")
def api_state():
    """Everything the app needs to render: settings, and the cycle if there is one."""
    profile = _profile()
    store = _rules()
    return {
        "firm": profile.to_dict(),
        "ledger_adapter": get_adapter(profile.ledger).name,
        "rules": {
            "total": len(store.rules),
            "starter": len(store.starter_rules),
            "firm": len([r for r in store.rules if r.decided_by
                         and r.decided_by != "Fynn starter pack"]),
        },
        "accounts": {
            "ledger": profile.ledger,
            "total": len(accounts_in_use()),
            "unmapped": len(_accounts().missing(accounts_in_use())),
        },
        "ledgers": connections.status(WORKSPACE_ID),
        "cycle": _cycle.digest() if _cycle else None,
    }


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), reported: str = Form("")):
    """Take one settlement export and fold it into the open cycle."""
    filename = file.filename or "upload.csv"
    raw = await file.read()
    try:
        cycle = _ingest(raw, filename, reported)
    except SettlementParseError as exc:
        raise HTTPException(422, str(exc))
    d = cycle.digest()
    print(f"\n[Upload] {filename} → cycle {d['cycle']}, {d['open_exceptions']} open exception(s)")
    return {"filename": filename, "cycle": d["cycle"], "digest": d}


@app.post("/api/cycle/sample")
def api_load_sample():
    """Load the built-in sample cycle covering the three known edge cases."""
    global _cycle
    profile = _profile()
    _cycle = Cycle(
        lines=sample_lines(), reported_payouts=dict(REPORTED_PAYOUTS),
        store=_rules(), prior_cycles=prior_cycle_lines(),
        firm=profile.firm, firm_id=WORKSPACE_ID,
    )
    _cycle.run()
    return _cycle.digest()


@app.delete("/api/cycle")
def api_clear_cycle():
    """Close the open cycle without posting. Rules already learned are kept."""
    global _cycle
    _cycle = None
    return {"cycle": None}


@app.get("/api/digest")
def api_digest():
    return _require_cycle().digest()


@app.get("/api/exception/opening")
def api_opening(key: str):
    """First message in the drawer. Deterministic — no model call."""
    cycle = _require_cycle()
    exc = _find_exception(cycle, key)
    # Ask the investigator only where the deterministic layer found nothing. It
    # is a suggestion either way, never a decision.
    if exc.evidence is None:
        exc.evidence = investigate(exc)
    return {"key": key, "message": opening_message(exc)}


@app.post("/api/exception/chat")
def api_chat(req: ChatRequest):
    """Follow-up questions about one exception. This is the only LLM surface
    the accountant talks to, and it cannot approve anything."""
    cycle = _require_cycle()
    exc = _find_exception(cycle, req.key)
    return {"message": reply(exc, req.history, cycle.lines, cycle.prior)}


@app.post("/api/approve")
def api_approve(req: ApproveRequest):
    """Approve from the app.

    The actor defaults to the name in firm settings — the audit trail should
    name whoever the firm said signs off, not whoever happens to be at the
    keyboard on a page with no login.
    """
    cycle = _require_cycle()
    actor = (req.actor or "").strip() or _profile().approver()
    exc = _find_exception(cycle, req.key)
    side = req.side or (Side.CREDIT if exc.amount > 0 else Side.DEBIT)
    cycle.approve(req.key, req.account, side, actor, req.save_rule, req.scope)
    return cycle.digest()


@app.post("/api/post")
def api_post():
    return _post_cycle()


@app.get("/api/journal/{platform}")
def api_journal(platform: str):
    cycle = _require_cycle()
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


class AccountMapRequest(BaseModel):
    # {"Marketing Expense": {"code": "6200", "name": "Advertising"}, ...}
    mapping: dict[str, dict]
    ledger: Optional[str] = None


@app.get("/api/accounts")
def api_get_accounts(ledger: Optional[str] = None):
    """Every account a posting would touch, with wherever it is mapped to."""
    target = (ledger or _profile().ledger or "dry-run").lower()
    names = accounts_in_use()
    amap = _accounts(target)
    return {
        "ledger": target,
        "accounts": [
            {"account": n, **amap.as_dict([n])[n]} for n in names
        ],
        "unmapped": amap.missing(names),
    }


@app.get("/api/accounts/chart")
def api_chart(ledger: Optional[str] = None):
    """The connected ledger's own chart of accounts, plus a suggested match.

    Typing codes by hand is the tedious, error-prone part of setup. With the
    ledger connected its chart can be read, so the mapping becomes a choice
    from a list — and each row arrives pre-selected where the match is
    unambiguous. A suggestion is never saved on its own.
    """
    target = (ledger or _profile().ledger or "dry-run").lower()
    connection = connections.get(WORKSPACE_ID, target)
    if connection is None:
        return {"ledger": target, "connected": False, "chart": [], "suggestions": {}}
    try:
        chart = fetch_chart(target, connection)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the chart of accounts: {exc}")

    amap = _accounts(target)
    suggestions = {}
    for account in accounts_in_use():
        if amap.get(account) is None:
            hit = suggest(account, chart)
            if hit:
                suggestions[account] = hit
    return {"ledger": target, "connected": True, "chart": chart,
            "suggestions": suggestions}


@app.put("/api/accounts")
def api_save_accounts(req: AccountMapRequest):
    target = (req.ledger or _profile().ledger or "dry-run").lower()
    amap = _accounts(target)
    for account, value in req.mapping.items():
        amap.set(account, (value or {}).get("code", ""), (value or {}).get("name", ""))
    return api_get_accounts(target)


OAUTH_STATE_COOKIE = "fynn_oauth_state"


@app.get("/oauth/{ledger}/connect", include_in_schema=False)
def oauth_connect(ledger: str):
    """Send the accountant to the ledger's consent screen."""
    try:
        provider = oauth.get_provider(ledger)
        # Signed, single-use, and checked on the way back: without it, a link
        # from anywhere could complete a connection into this workspace.
        state = _sign(int(time.time()) + 600)
        url = oauth.authorize_url(provider, base_url(), state)
    except oauth.OAuthError as exc:
        raise HTTPException(400, str(exc))

    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        OAUTH_STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
        secure=bool(os.getenv("RAILWAY_PUBLIC_DOMAIN")),
    )
    return response


@app.get("/oauth/{ledger}/callback", include_in_schema=False)
def oauth_callback(
    request: Request,
    ledger: str,
    code: str = "",
    state: str = "",
    realmId: str = "",
    error: str = "",
):
    """Consent came back. Swap the code for tokens and remember them."""
    def fail(message: str):
        return RedirectResponse(f"/?ledger_error={quote(message, safe='')}", status_code=303)

    if error:
        return fail(f"{ledger} returned: {error}")
    if not code:
        return fail("No authorisation code came back.")

    expected = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not state or not expected or not secrets.compare_digest(state, expected) \
            or not _valid_session(state):
        return fail("That sign-in did not come from here, or it expired. Try again.")

    try:
        provider = oauth.get_provider(ledger)
        tokens = oauth.exchange_code(provider, code, base_url(), realm_id=realmId or None)
    except oauth.OAuthError as exc:
        return fail(str(exc))

    try:
        connections.store(WORKSPACE_ID, provider.name, tokens)
    except Exception as exc:
        # Do not point the profile at a ledger we cannot reach tokens for.
        return fail(str(exc))

    # Point the firm at what they just connected, so posting goes there.
    profile = _profile()
    profile.ledger = provider.name
    _profiles.save(profile)

    response = RedirectResponse(f"/?connected={provider.name}", status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    return response


@app.post("/api/ledger/disconnect")
def api_disconnect(ledger: str):
    """Forget a ledger's tokens. The account mapping is kept."""
    connections.forget(WORKSPACE_ID, ledger)
    profile = _profile()
    if profile.ledger == ledger:
        profile.ledger = "dry-run"
        _profiles.save(profile)
    return connections.status(WORKSPACE_ID)


@app.get("/api/ledger/status")
def api_ledger_status():
    # Each ledger's redirect URI is returned in full rather than as a pattern
    # to fill in. Both providers match it byte-for-byte, so a URI retyped from
    # a template is the likeliest way a connection fails — and the error comes
    # back from the provider, after the redirect, where it is hard to read.
    status = connections.status(WORKSPACE_ID)
    for name, row in status.items():
        row["redirect_uri"] = oauth.redirect_uri(base_url(), oauth.get_provider(name))
    return {"base_url": base_url(), "ledgers": status}


@app.get("/api/diagnostics")
def api_diagnostics():
    """Whether anything is actually being persisted.

    Every database write is deliberately best-effort, so a close keeps running
    through an outage — which also means a misconfiguration is invisible. This
    endpoint is how you tell the difference.
    """
    storage = diagnose()
    return {
        "storage": storage,
        "warning": None if storage.get("connected") else (
            "Rules, settings, account mappings and retained files exist only in "
            "this process. A restart or redeploy loses them."
        ),
    }


@app.get("/api/rules")
def api_rules():
    store = _rules()
    return {"count": len(store.rules), "rules": store.export()}


@app.get("/api/audit.json")
def api_audit_json():
    return {"records": _require_cycle().trail.to_dicts()}


@app.get("/api/audit", response_class=PlainTextResponse)
def api_audit_csv():
    return _require_cycle().trail.to_csv()


@app.get("/api/settings")
def api_get_settings():
    return _profile().to_dict()


@app.put("/api/settings")
def api_save_settings(req: SettingsRequest):
    profile = _profile()
    profile.firm = req.firm.strip() or "Your firm"
    profile.actor = req.actor.strip()
    profile.platforms = [p for p in req.platforms if p in SUPPORTED_PLATFORMS] \
        or list(SUPPORTED_PLATFORMS)
    profile.ledger = req.ledger if req.ledger in SUPPORTED_LEDGERS else "dry-run"
    profile.onboarding_step = None
    _profiles.save(profile)
    if _cycle is not None:
        _cycle.firm = profile.firm
    return profile.to_dict()


def _find_exception(cycle: Cycle, key: str):
    for result in cycle.run().values():
        for exc in result.exceptions:
            if exc.key == key:
                return exc
    raise HTTPException(404, f"No open exception with key {key}")


# ── Script-facing endpoints ───────────────────────────────────────────────────
# The same operations without the app, for curl and for anything the page does
# not cover yet.

@app.post("/cycle/sample")
def load_sample():
    return api_load_sample()


@app.post("/cycle/upload")
async def upload(file: UploadFile = File(...), reported: str = Form("")):
    """Upload a settlement export.

    Canonical columns: platform,cycle,order,label,amount,date — but the parser
    also accepts a marketplace's own header names and infers the platform from
    the file. `reported` is a semicolon list, e.g. "Lazada=1038;Shopee=725";
    omit it when the file states the payout on its own row.
    """
    raw = await file.read()
    try:
        cycle = _ingest(raw, file.filename or "upload.csv", reported)
    except SettlementParseError as exc:
        raise HTTPException(400, str(exc))
    return cycle.digest()


@app.get("/digest")
def digest():
    return _require_cycle().digest()


@app.post("/investigate/{key:path}")
def investigate_exception(key: str):
    """Ask the investigator for a suggestion on one exception.

    Returns a suggestion with a confidence score. It is never applied
    automatically — an accountant approves separately.
    """
    cycle = _require_cycle()
    exc = _find_exception(cycle, key)
    ev = investigate(exc)
    if ev is None:
        return {"key": key, "evidence": None,
                "note": "No suggestion available. Set ANTHROPIC_API_KEY to enable the investigator."}
    return {"key": key, "evidence": ev.model_dump(mode="json")}


@app.post("/approve")
def approve(req: ApproveRequest):
    return api_approve(req)


@app.post("/post")
def post_entries():
    return _post_cycle()


@app.get("/rules")
def rules():
    return api_rules()


@app.get("/audit", response_class=PlainTextResponse)
def audit_csv():
    """Working paper export."""
    return _require_cycle().trail.to_csv()
