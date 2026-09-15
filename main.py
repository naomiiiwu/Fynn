"""Fynn — settlement reconciliation for Southeast Asian marketplaces.

FastAPI entry point. One workspace, one web application:

  /            the app — upload, review, approve, post, settings
  /api/*       everything the app calls
  /cycle/*, /approve, /post, /audit
               the same operations for scripts

The engine is utils.formatter.Cycle. The page and the JSON are the same
reconciliation rendered twice, never two code paths that can drift.

Each account owns one workspace and sees nothing outside it. Every store
already took a firm id, so isolation is a matter of which id is passed: a
user's id is their workspace id. See the Accounts and access section below.
"""

import base64
import os
from datetime import datetime, timezone
import re
import secrets
import sys
import threading
import time
from contextvars import ContextVar
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
from agents.triage import triage
from data.sample_settlements import REPORTED_PAYOUTS, prior_cycle_lines, sample_lines
from models.account_map import AccountMap, suggest
from models.firm_profile import (
    POSTING_ACCOUNTS,
    SUPPORTED_LEDGERS,
    SUPPORTED_PLATFORMS,
    FirmProfile,
    FirmProfileStore,
)
from models.user import User, UserStore
from models.transaction import Platform, Side
from services.audit import working_paper
from services.classification import RuleStore
from services.csv_parser import SettlementParseError, parse_reported, parse_settlement_csv
from services import connections, identity, migrate, oauth, settlements
from services.database import (
    diagnose,
    save_posted_entry,
    save_resolution,
    save_settlement,
    save_settlement_file,
    update_settlement_reported,
)
from services.ledger import (
    delete_draft_invoice, fetch_chart, fetch_invoice_statuses, get_adapter)
from utils.formatter import Cycle

app = FastAPI(
    title="Fynn",
    description="Settlement reconciliation for Shopee, Lazada and TikTok Shop",
    version="0.4.0",
)

STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
def _apply_schema() -> None:
    """Bring the database up to date before serving anything.

    Nobody should have to notice that a release needs a new table. See
    services/migrate.py for why running this on every boot is safe.
    """
    migrate.apply()

# ── Accounts and access ───────────────────────────────────────────────────────
# Every account owns one workspace and sees nothing outside it. A user's id is
# their workspace id, so the scoping the schema already had — every table keyed
# on firm_id — becomes per-account without the schema learning about users.
#
# Two ways in, and an account may have both: an email address with a password,
# or Google. Whichever is used, the session cookie names a user, and the
# workspace follows from that rather than from anything the browser sends.

_users = UserStore()

SESSION_COOKIE = "fynn_session"
OPEN_PATHS = {"/health", "/login", "/signup", "/logout", "/auth/providers",
              "/auth/google", "/auth/google/callback"}

# Set by the middleware before the route runs, so the helpers below can resolve
# the workspace without every endpoint having to accept and forward it. A
# ContextVar rather than a global: concurrent requests belong to different
# people, and a global would hand one of them the other's books.
_current_user: ContextVar[Optional[str]] = ContextVar("fynn_user", default=None)


def _user_from_request(request: Request) -> Optional[User]:
    """Whoever this request is, by session cookie or by HTTP Basic.

    Basic is kept so scripts and curl keep working, but it now means a real
    account's email and password rather than one shared secret.
    """
    # Cheap when there is nothing pending, which is the normal case. It only
    # does work in the window between an account being made and its table
    # existing — exactly the window where losing it would be unrecoverable.
    if _users.pending:
        _users.flush()

    user_id = identity.read_session(request.cookies.get(SESSION_COOKIE, ""))
    if user_id:
        return _users.get(user_id)

    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        email, _, password = base64.b64decode(encoded).decode("utf-8").partition(":")
    except Exception:
        return None
    user = _users.by_email(email)
    if user and user.password_hash and identity.verify_password(password, user.password_hash):
        return user
    # Spend the same work on a miss as on a hit, so response time does not
    # separate "no such account" from "wrong password".
    identity.verify_password(password, identity.hash_password("decoy"))
    return None


@app.middleware("http")
async def require_account(request: Request, call_next):
    if request.url.path in OPEN_PATHS:
        return await call_next(request)

    user = _user_from_request(request)
    if user is None:
        # A browser gets the sign-in page; anything else gets the Basic
        # challenge, so curl and scripts get something they can act on.
        if "text/html" in request.headers.get("accept", ""):
            wanted = request.url.path
            if request.url.query:
                wanted += "?" + request.url.query
            return RedirectResponse(f"/login?next={quote(wanted, safe='')}",
                                    status_code=303)
        return Response(
            status_code=401,
            content="Sign in to Fynn first.",
            headers={"WWW-Authenticate": 'Basic realm="Fynn", charset="UTF-8"'},
        )

    token = _current_user.set(user.id)
    try:
        return await call_next(request)
    finally:
        _current_user.reset(token)


def _session_cookie(response, user: User):
    response.set_cookie(
        SESSION_COOKIE, identity.issue_session(user.id),
        max_age=identity.SESSION_DAYS * 86400, httponly=True, samesite="lax",
        # Railway terminates TLS, so the cookie travels over https there. Left
        # off for local http, where it would simply never be sent back.
        secure=bool(os.getenv("RAILWAY_PUBLIC_DOMAIN")),
    )
    return response


def _safe_next(target: str) -> str:
    """Only ever redirect within this site."""
    return target if target.startswith("/") and not target.startswith("//") else "/"


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"))


@app.get("/signup", include_in_schema=False)
def signup_page():
    return FileResponse(os.path.join(STATIC, "signup.html"))


@app.post("/signup", include_in_schema=False)
def signup(email: str = Form(...), password: str = Form(...),
           name: str = Form(""), next: str = Form("/")):
    address = (email or "").strip().lower()
    if "@" not in address or "." not in address.split("@")[-1]:
        return RedirectResponse("/signup?error=email", status_code=303)
    problem = identity.password_problem(password)
    if problem:
        return RedirectResponse(f"/signup?error={quote(problem, safe='')}",
                                status_code=303)
    if _users.by_email(address):
        return RedirectResponse("/signup?error=taken", status_code=303)

    user = _users.create(address, name=name.strip(),
                         password_hash=identity.hash_password(password))
    # Create the workspace row now, so the first rule or mapping written into it
    # has its foreign-key parent and does not fail silently.
    _profiles.get_or_create(user.workspace_id)
    return _session_cookie(RedirectResponse(_safe_next(next), status_code=303), user)


@app.post("/login", include_in_schema=False)
def login(email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    user = _users.by_email(email)
    if not user or not user.password_hash or \
            not identity.verify_password(password, user.password_hash):
        # One message for both cases: naming which half was wrong tells an
        # attacker which addresses have accounts.
        return RedirectResponse("/login?error=1", status_code=303)
    return _session_cookie(RedirectResponse(_safe_next(next), status_code=303), user)


@app.get("/auth/providers", include_in_schema=False)
def auth_providers():
    """Which sign-in routes this deployment can actually complete."""
    return {"google": identity.google_configured()}


@app.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Google sign-in ────────────────────────────────────────────────────────────

GOOGLE_STATE_COOKIE = "fynn_google_state"


@app.get("/auth/google", include_in_schema=False)
def google_start(next: str = "/"):
    if not identity.google_configured():
        return RedirectResponse("/login?error=nogoogle", status_code=303)
    state = identity.new_state()
    url = identity.google_authorize_url(base_url(), state)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(GOOGLE_STATE_COOKIE, f"{state}|{_safe_next(next)}",
                        max_age=600, httponly=True, samesite="lax",
                        secure=bool(os.getenv("RAILWAY_PUBLIC_DOMAIN")))
    return response


@app.get("/auth/google/callback", include_in_schema=False)
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    def fail(message: str):
        return RedirectResponse(f"/login?error={quote(message, safe='')}",
                                status_code=303)

    if error:
        return fail(f"Google returned: {error}")
    expected, _, wanted = request.cookies.get(GOOGLE_STATE_COOKIE, "").partition("|")
    if not code or not state or not expected or \
            not secrets.compare_digest(state, expected):
        return fail("That sign-in did not come from here, or it expired. Try again.")

    try:
        person = identity.google_identity(code, base_url())
    except identity.AuthError as exc:
        return fail(str(exc))

    # Match on the Google subject id first: it survives the person changing the
    # address on their Google account, which the email does not.
    user = _users.by_google(person["sub"]) or _users.by_email(person["email"])
    if user is None:
        user = _users.create(person["email"], name=person["name"],
                             google_sub=person["sub"])
        _profiles.get_or_create(user.workspace_id)
    elif not user.google_sub:
        # An existing password account signing in with Google for the first
        # time: link the two rather than stranding them in a second workspace.
        user.google_sub = person["sub"]
        if not user.name:
            user.name = person["name"]
        _users.save(user)

    response = RedirectResponse(_safe_next(wanted or "/"), status_code=303)
    response.delete_cookie(GOOGLE_STATE_COOKIE)
    return _session_cookie(response, user)


# ── State ─────────────────────────────────────────────────────────────────────
# One Workspace per account, holding everything that is not in the database:
# the reconciled months above all. Rules and mappings are written through to
# Supabase as they are learned; a month is rebuilt from its retained files when a
# process first needs it, and every settlement it produces is recorded so the
# list of them survives without rebuilding anything.

_profiles = FirmProfileStore()

# How many earlier months a refund is traced back through. A marketplace refund
# lands weeks after the sale, rarely more than a quarter.
PRIOR_MONTHS = 3


def _when(value) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class Workspace:
    """Everything one account can see, and nothing another can."""

    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id
        # Every month this process has reconciled, by month. Several at once:
        # February arriving must not push an unfinished January aside.
        self._cycles: dict[str, Cycle] = {}
        # Months already looked for and not found, so a month with nothing
        # retained does not re-query storage on every request.
        self._absent: set[str] = set()
        # The month the review screen is on. Restored from the profile once per
        # process; a cycle deliberately closed must not come back.
        self._current: Optional[str] = None
        self._sample: Optional[Cycle] = None
        self._restored = False
        self._rules: Optional[RuleStore] = None
        # Keyed by ledger: a Xero code and a QuickBooks id are different things,
        # so a firm that switches ledgers maps again rather than inheriting the
        # wrong codes.
        self._account_maps: dict[str, AccountMap] = {}

    SAMPLE = "sample"

    @property
    def cycle(self) -> Optional[Cycle]:
        """The month under review, rebuilt from the retained files if need be.

        Source files are kept for the statutory period anyway, so the data to
        rebuild was always there — nothing read it back, and a redeploy looked
        like the upload had been lost when only the reconciliation had. Rules
        are loaded from storage too, so the same decisions re-apply and the same
        exceptions resolve.
        """
        if not self._restored:
            self._restored = True
            self._current = self.profile().open_cycle
        if self._current == self.SAMPLE:
            return self._sample
        return self.month(self._current) if self._current else None

    @cycle.setter
    def cycle(self, value: Optional[Cycle]) -> None:
        self._restored = True
        if value is None:
            self._current = None
            return
        if getattr(value, "sample", False):
            # Held apart from the real months. It is named 2026-01 like a real
            # January, and must never stand in for one.
            self._sample, self._current = value, self.SAMPLE
            return
        self._cycles[value.cycle] = value
        self._absent.discard(value.cycle)
        self._current = value.cycle

    def month(self, month: str) -> Optional[Cycle]:
        """One month's reconciliation, from memory or from its retained files."""
        if not month:
            return None
        if month not in self._cycles and month not in self._absent:
            rebuilt = self._rebuild(month)
            if rebuilt is None:
                self._absent.add(month)
                return None
            self._cycles[month] = rebuilt
        return self._cycles.get(month)

    def loaded(self) -> list[Cycle]:
        return list(self._cycles.values())

    def open_month(self, month: str) -> Optional[Cycle]:
        """Make a month the one under review, and remember it across a restart."""
        cycle = self.month(month)
        if cycle is None:
            return None
        self.cycle = cycle
        profile = self.profile()
        if profile.open_cycle != month:
            profile.open_cycle = month
            _profiles.save(profile)
        return cycle

    def prior_lines(self, month: str, files: Optional[list[dict]] = None) -> list:
        """Settlement lines from the few months before this one.

        A refund in February for a January sale has no sale in February, and
        without January to look in it is raised as an orphan with nothing to
        say about it. Only lines are wanted, so the months are parsed, not
        reconciled.
        """
        if files is None:
            try:
                from services.database import load_settlement_files
                files = load_settlement_files(self.id)
            except Exception:
                return []
        earlier = sorted({r.get("cycle") for r in files
                          if r.get("cycle") and r["cycle"] < month})[-PRIOR_MONTHS:]
        lines = []
        for row in files:
            if row.get("cycle") not in earlier:
                continue
            if row["cycle"] in self._cycles:
                continue            # taken from memory below, with its later files
            try:
                parsed = parse_settlement_csv(
                    (row.get("csv_data") or "").encode("utf-8"),
                    row.get("filename") or "", default_cycle=row["cycle"])
                lines.extend(parsed.lines)
            except SettlementParseError:
                continue
        for m in earlier:
            if m in self._cycles:
                lines.extend(self._cycles[m].lines)
        return lines

    def _rebuild(self, month: str) -> Optional[Cycle]:
        profile = self.profile()
        try:
            from services.database import load_settlement_files
            everything = load_settlement_files(self.id)
        except Exception as exc:
            print(f"  [Cycle] Could not read retained files: {exc}")
            return None
        rows = [r for r in everything if r.get("cycle") == month]
        if not rows:
            return None

        rebuilt: Optional[Cycle] = None
        # In ingest order, so a later file folds into the earlier one exactly as
        # it did the first time.
        for row in sorted(rows, key=lambda r: r.get("ingested_at") or ""):
            raw = (row.get("csv_data") or "").encode("utf-8")
            try:
                parsed = parse_settlement_csv(
                    raw, row.get("filename") or "",
                    default_cycle=month,
                    known_orders={l.order_id: l.platform
                                  for l in (rebuilt.lines if rebuilt else []) if l.order_id},
                )
            except SettlementParseError as exc:
                print(f"  [Cycle] Could not re-read {row.get('filename')}: {exc}")
                continue
            payouts = dict(parsed.reported_payouts)
            payouts.update(parse_reported(row.get("reported") or ""))
            # The rows' own total too, exactly as at upload. Leaving it out
            # brought every cycle back from a redeploy with a payout of 0.00 and
            # the whole deposit reopened as an unexplained residual.
            if rebuilt is None:
                rebuilt = Cycle(
                    lines=parsed.lines, reported_payouts=payouts, store=self.rules(),
                    prior_cycles=self.prior_lines(month, everything),
                    cycle=month, firm=profile.firm, firm_id=self.id,
                    stated_totals=parsed.stated_totals,
                )
            else:
                rebuilt.add_lines(parsed.lines, payouts, parsed.stated_totals)

        if rebuilt is not None:
            history = []
            try:
                from services.database import load_posted_entries
                for row in load_posted_entries(self.id, month):
                    rebuilt.posted.append({
                        "reference": row.get("reference", ""),
                        "platform": row.get("platform", ""),
                        "payout": "",
                        "adapter": row.get("adapter", ""),
                        "at": row.get("posted_at", ""),
                        "actor": row.get("actor", ""),
                    })
                    history.append((_when(row.get("posted_at")), "post",
                                    f"{row.get('reference', '')} sent to {row.get('adapter', '')}",
                                    row.get("actor") or "Fynn"))
            except Exception as exc:
                print(f"  [Cycle] Could not read what was already posted: {exc}")
            # Decisions that never became rules, re-applied before the first
            # reconciliation so the close comes back as it was left.
            try:
                from services.database import load_resolutions
                for row in load_resolutions(self.id, month):
                    rebuilt.resolutions[row["key"]] = (
                        row["account"], Side(row["side"]))
                    history.append((_when(row.get("decided_at")), "decision",
                                    f"Approved — {row['key']} posts to {row['account']}",
                                    row.get("actor") or "Fynn"))
            except Exception as exc:
                print(f"  [Cycle] Could not re-apply decisions: {exc}")
            first = min((t for t in (_when(r.get("ingested_at")) for r in rows) if t),
                        default=None)
            now = datetime.now(timezone.utc)
            rebuilt.trail.restore(first or now,
                                  [(at or now, *rest) for at, *rest in history])
            rebuilt.run()
            print(f"  [Cycle] Resumed {month} for {self.id} "
                  f"from {len(rows)} retained file(s).")
            self.record(rebuilt)
        return rebuilt

    # ── settlements ──────────────────────────────────────────────────────────

    def per_payout(self) -> bool:
        """Whether this firm's ledger takes one document per payout."""
        return get_adapter(self.profile().ledger, preview_for="xero").per_payout

    def record(self, cycle: Cycle) -> list[dict]:
        """Write every settlement in a month as it now reconciles.

        Only the computed half of each row. What was sent is written by the post
        and never here, so reconciling again cannot erase the record of a post.
        """
        live = [settlements.computed_row(entry, result, cycle)
                for entry, result in settlements.documents(cycle, self.per_payout())]
        if getattr(cycle, "sample", False):
            return live            # the sample is a demonstration, not a close
        try:
            from services.database import delete_settlement, load_settlements, save_settlement
            current = {r["reference"] for r in live}
            for row in load_settlements(self.id, cycle.cycle):
                # A document the month no longer produces — one payout split into
                # several by a later file. Kept if it was sent: it is in the
                # ledger whatever Fynn now thinks.
                if row["reference"] not in current and not row.get("posted_at"):
                    delete_settlement(self.id, row["reference"])
            for row in live:
                save_settlement(self.id, {
                    **row, "updated_at": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            print(f"  [Settlements] Could not record {cycle.cycle}: {exc}")
        return live

    def settlement_rows(self) -> list[dict]:
        """Every settlement this firm has, stored and live, newest first.

        A month with retained files but no settlement rows was uploaded before
        settlements were recorded; it is rebuilt once here, so a firm's history
        appears in the list without anyone re-uploading it.
        """
        try:
            from services.database import load_settlement_months, load_settlements
            stored = load_settlements(self.id)
            months = load_settlement_months(self.id)
        except Exception:
            stored, months = [], []
        known = {r.get("cycle") for r in stored}
        for month in months:
            if month not in known:
                self.month(month)
        if any(m not in known for m in months):
            try:
                from services.database import load_settlements
                stored = load_settlements(self.id)
            except Exception:
                pass
        live = [row for c in self.loaded() if not getattr(c, "sample", False)
                for row in (settlements.computed_row(e, r, c)
                            for e, r in settlements.documents(c, self.per_payout()))]
        rows = settlements.merge(stored, live)
        for row in rows:
            sent = _posted_now.get((self.id, row["reference"]))
            if sent and not row.get("posted_at"):
                row.update(sent)
        self._adopt_posts(rows)
        return sorted(rows, key=settlements.sort_key, reverse=True)

    def _adopt_posts(self, rows: list[dict]) -> None:
        """Show as sent whatever posted_entries says was sent.

        posted_entries is the record of every post, and it predates settlements.
        A settlement with no post of its own but a post in that record was sent
        before settlements were recorded — or while their table did not exist
        yet — and listing it as Ready to post invites a second copy into a
        client's books. So the record is read on every listing, not trusted to
        have been copied across once, and the copy is written back when it can
        be.
        """
        waiting = [r for r in rows if not r.get("posted_at")]
        if not waiting:
            return
        try:
            from services.database import load_posted_entries, save_settlement
            posts = load_posted_entries(self.id)            # newest first
        except Exception:
            return
        latest: dict[str, dict] = {}
        for post in posts:
            latest.setdefault(post.get("reference", ""), post)
        for row in waiting:
            post = latest.get(row["reference"])
            if post is None:
                continue
            lines = post.get("lines") or []
            sent = {
                "ledger": post.get("adapter", ""),
                # Xero was only ever sent drafts. The status check corrects this
                # the first time it runs.
                "ledger_status": "DRAFT" if post.get("adapter") == "xero" else "",
                "posted_total": settlements.total_of_lines(lines),
                "posted_lines": lines,
                "posted_at": post.get("posted_at") or datetime.now(timezone.utc).isoformat(),
                "posted_by": post.get("actor", ""),
            }
            row.update(sent)
            save_settlement(self.id, {"reference": row["reference"],
                                      "cycle": row.get("cycle", ""),
                                      "platform": row.get("platform", ""), **sent})

    def forget(self, month: str) -> None:
        """Drop a month from memory, so the next read rebuilds it from storage."""
        self._cycles.pop(month, None)
        self._absent.discard(month)

    def profile(self) -> FirmProfile:
        profile, _ = _profiles.get_or_create(self.id)
        return profile

    def rules(self) -> RuleStore:
        if self._rules is None:
            self._rules = RuleStore.for_firm(self.id)
        return self._rules

    def accounts(self, ledger: Optional[str] = None) -> AccountMap:
        ledger = (ledger or self.profile().ledger or "dry-run").lower()
        if ledger not in self._account_maps:
            self._account_maps[ledger] = AccountMap.for_firm(self.id, ledger)
        return self._account_maps[ledger]


_workspaces: dict[str, Workspace] = {}


def ws() -> Workspace:
    """The signed-in account's workspace."""
    user_id = _current_user.get()
    if user_id is None:
        raise HTTPException(401, "Sign in to Fynn first.")
    if user_id not in _workspaces:
        _workspaces[user_id] = Workspace(user_id)
    return _workspaces[user_id]


def current_user() -> Optional[User]:
    user_id = _current_user.get()
    return _users.get(user_id) if user_id else None


def base_url() -> str:
    """The app's own public URL, which the OAuth redirect must match exactly."""
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else "http://localhost:8000"


def _rules() -> RuleStore:
    return ws().rules()


def _accounts(ledger: Optional[str] = None) -> AccountMap:
    return ws().accounts(ledger)


def _profile() -> FirmProfile:
    return ws().profile()


def accounts_in_use() -> list[str]:
    """Every account name a posting would touch.

    Taken from the rules the firm has accumulated and from the open cycle's
    journals, so the mapping screen is useful before a cycle exists as well as
    during one. Clearing accounts are generated per platform rather than named
    in any rule, so they are added from settings.
    """
    space = ws()
    names = {r.account for r in space.rules().rules}
    for platform in space.profile().platforms:
        names.add(f"{platform} Clearing Account")
    if space.cycle is not None:
        for result in space.cycle.run().values():
            if result.journal:
                names.update(l.account for l in result.journal.lines)
    return sorted(names)


def _require_cycle() -> Cycle:
    cycle = ws().cycle
    if cycle is None:
        raise HTTPException(404, "No cycle is open. Upload a settlement file first.")
    return cycle


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.4.0"}


@app.get("/debug")
async def debug() -> dict:
    """Live state snapshot."""
    return {
        "cycle": {
            "period": ws().cycle.cycle,
            "lines": len(ws().cycle.lines),
            "open_exceptions": len(ws().cycle.open_exceptions()),
            "platforms": sorted(p.value for p in ws().cycle._results),
        } if ws().cycle else None,
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
    space = ws()
    profile = space.profile()

    # The same file twice is never a second settlement. Folding it in again
    # changes nothing — its lines are already there — but storing it again
    # leaves two copies behind one figure, and a reviewer tracing the figure
    # back finds both. Compared by content, so a browser's "file (1).csv" is
    # caught as well as the same name.
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        from services.database import load_settlement_files
        earlier = next((r for r in load_settlement_files(space.id)
                        if (r.get("csv_data") or "") == text), None)
    except Exception:
        earlier = None
    if earlier is not None:
        same = earlier.get("filename") == filename
        raise HTTPException(409, (
            f"Already uploaded{'' if same else ' as ' + str(earlier.get('filename'))}"
            f" for {earlier.get('cycle')}. Nothing was added."))

    # An adjustments file names no marketplace anywhere, but the orders it
    # refers to are already in the open cycle. Offering those lets it be
    # attributed instead of rejected.
    known_orders = {
        l.order_id: l.platform for l in space.cycle.lines if l.order_id
    } if space.cycle is not None else {}

    # A firm that settles on exactly one platform has already told us which.
    default_platform = (
        Platform(profile.platforms[0]) if len(profile.platforms) == 1 else None
    )

    parsed = parse_settlement_csv(
        raw, filename=filename, default_platform=default_platform,
        known_orders=known_orders,
    )
    # Three sources, weakest first, so a stronger one always wins:
    #   what the file's own rows add up to — always available, and the figure an
    #     accountant would otherwise copy out of the same file by hand;
    #   a payout the platform states on a row of its own — Lazada does this;
    #   what the firm typed, which is the only one that can come from a bank.
    # The first is kept apart from the other two: it is a total for this file
    # only, so a later file adds to it instead of replacing the cycle's payout.
    payouts = dict(parsed.reported_payouts)
    payouts.update(parse_reported(reported))

    # The month this file belongs to, wherever it was: in memory, retained from
    # an earlier upload, or new. A file for another month joins that month and
    # leaves the one on screen exactly as it was — it stays in the settlements
    # list, with whatever it still needs.
    existing = space.month(parsed.cycle)
    if existing is None:
        target = Cycle(
            lines=parsed.lines, reported_payouts=payouts, store=_rules(),
            prior_cycles=space.prior_lines(parsed.cycle),
            cycle=parsed.cycle, firm=profile.firm, firm_id=space.id,
            stated_totals=parsed.stated_totals,
        )
        target.run()
    else:
        target = existing
        target.add_lines(parsed.lines, payouts, parsed.stated_totals)

    save_settlement_file(space.id, filename, parsed.cycle, raw, len(parsed.lines),
                         reported=reported)
    space.cycle = target
    # Remember which cycle is open, so it can be rebuilt after a restart.
    if profile.open_cycle != parsed.cycle:
        profile.open_cycle = parsed.cycle
        _profiles.save(profile)
    space.record(target)
    space.auto_posted = _auto_post(target)
    _start_proposing(target)
    return target


def _send(cycle: Cycle, entries: list, replaces: Optional[dict] = None) -> list[dict]:
    """Send documents to the firm's ledger and record each one as posted.

    The one path every post takes — a whole month or one settlement — so the
    trail, posted_entries and the settlement row cannot disagree about what
    went out.
    """
    space = ws()
    profile = space.profile()
    adapter = get_adapter(
        profile.ledger, _accounts(profile.ledger),
        connections.get(space.id, profile.ledger),
        preview_for="xero",
    )
    actor = profile.approver()
    out = []
    for entry in entries:
        adapter.replaces = (replaces or {}).get(entry.reference, "")
        try:
            result = adapter.post(entry)
        except NotImplementedError as exc:
            # The live adapters prepare the payload but cannot send it yet. That
            # is a configuration state, not a server fault.
            raise HTTPException(501, str(exc).split(". Payload prepared")[0])
        except RuntimeError as exc:
            # Unmapped accounts, or missing credentials. Both are things the
            # firm can fix, and the message says which.
            raise HTTPException(409, str(exc))
        out.append(result)

        # Record what was actually sent, one line per document. A trail that
        # names the cycle entry while four invoices went out is a trail of
        # something that did not happen.
        where = f" ({entry.payout})" if entry.payout else ""
        cycle.trail.add(
            "post", f"{entry.reference}{where} sent to {adapter.name}", actor=actor
        )
        now = datetime.now(timezone.utc).isoformat()
        save_posted_entry(
            space.id, cycle.cycle, entry, adapter.name, actor, cycle.trail.to_csv()
        )
        cycle.posted = [p for p in cycle.posted if p.get("reference") != entry.reference]
        cycle.posted.append({
            "reference": entry.reference,
            "platform": entry.platform.value,
            "payout": entry.payout,
            "adapter": adapter.name,
            "at": now,
            "actor": actor,
        })
        # The Xero document id is what lets Fynn ask later whether the draft was
        # approved and matched to the deposit. Xero returns it once, here.
        lines = [{"account": l.account, "side": l.side.value, "amount": round(l.amount, 2)}
                 for l in entry.lines]
        save_settlement(space.id, {
            "reference": entry.reference, "cycle": cycle.cycle,
            "platform": entry.platform.value,
            "ledger": adapter.name,
            "ledger_id": result.get("ledger_id", "") or "",
            "ledger_status": (result.get("ledger_status", "") or "").upper()
                             if adapter.name != "dry-run" else "",
            "document": result.get("document", "") or "",
            "posted_total": settlements.total_of(entry),
            "posted_lines": lines,
            "posted_at": now,
            "posted_by": actor,
        })
        _posted_now[(space.id, entry.reference)] = {
            "ledger": adapter.name, "ledger_id": result.get("ledger_id", "") or "",
            "ledger_status": (result.get("ledger_status", "") or "").upper()
                             if adapter.name != "dry-run" else "",
            "document": result.get("document", "") or "",
            "posted_total": settlements.total_of(entry), "posted_lines": lines,
            "posted_at": now, "posted_by": actor,
        }
    space.record(cycle)
    return out


# What was sent in this process, by workspace and reference. Storage is the
# record; this is so a firm running without it still sees what it just posted.
_posted_now: dict[tuple[str, str], dict] = {}


def _auto_post(cycle: Optional[Cycle]) -> Optional[dict]:
    """Send whatever in this month has just become ready, if the firm asked.

    Called after anything that can make a settlement ready: an approval, a
    payout supplied, a file uploaded. Sends the same documents Post would, one
    path, so nothing about what goes out differs from pressing the button.

    Ready means never sent, or a draft that no longer matches what was sent. A
    document voided or deleted in Xero is not sent again: somebody removed it
    there on purpose.

    Never raises. A post that cannot go — an unmapped account, a disconnected
    ledger — must not undo the approval that triggered it; it is reported back
    and noted in the trail, and the settlement stays Ready to post.
    """
    if cycle is None or getattr(cycle, "sample", False):
        return None
    space = ws()
    profile = space.profile()
    if not profile.auto_post or profile.ledger != "xero":
        return None

    ready = {r["reference"] for r in
             (settlements.present(row) for row in space.settlement_rows())
             if r["cycle"] == cycle.cycle and r["status"] in ("ready", "changed")}
    entries = [e for e, _r in settlements.documents(cycle, space.per_payout())
               if e.reference in ready]
    if not entries:
        return None
    if connections.get(space.id, "xero") is None:
        why = "Xero is not connected. Reconnect it in Settings."
    else:
        try:
            _send(cycle, entries)
            return {"sent": [e.reference for e in entries]}
        except HTTPException as exc:
            why = str(exc.detail)
    cycle.trail.add("post", f"Automatic post held — {why}", actor="Fynn")
    return {"sent": [], "error": why,
            "waiting": [e.reference for e in entries]}


def _post_cycle() -> dict:
    """Post every platform's journal. Refuses while anything is open.

    Documents already approved in the ledger are left alone: they belong to the
    accountant now, and sending them again would fail or, worse, not.
    """
    cycle = _require_cycle()
    results = cycle.run()
    blocked = [p.value for p, r in results.items() if not r.ties_out]
    if blocked:
        raise HTTPException(
            409, f"Unresolved exceptions on: {', '.join(blocked)}. Resolve before posting."
        )

    rows = {r["reference"]: r for r in ws().settlement_rows() if r.get("cycle") == cycle.cycle}
    entries, skipped, replaces = [], [], {}
    for entry, _result in settlements.documents(cycle, _adapter_per_payout()):
        row = rows.get(entry.reference, {})
        sendable, why = settlements.ledger_allows(row)
        if not sendable:
            skipped.append({"reference": entry.reference, "why": why})
            continue
        if (row.get("ledger_status") or "").upper() in settlements.GONE_FROM_LEDGER:
            replaces[entry.reference] = row.get("ledger_id") or "voided"
        entries.append(entry)
    out = _send(cycle, entries, replaces)
    return {"adapter": get_adapter(_profile().ledger).name, "entries": out,
            "skipped": skipped}


def _adapter_per_payout() -> bool:
    return ws().per_payout()


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
    # Left out by setup, which saves settings as it goes and must not turn this
    # off each time it does.
    auto_post: Optional[bool] = None


@app.get("/api/state")
def api_state():
    """Everything the app needs to render: settings, and the cycle if there is one."""
    profile = _profile()
    store = _rules()
    user = current_user()
    return {
        "user": user.to_dict() if user else None,
        "posting_accounts": list(POSTING_ACCOUNTS),
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
        "ledgers": connections.status(ws().id),
        "cycle": ws().cycle.digest() if ws().cycle else None,
    }


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), reported: str = Form("")):
    """Take one settlement export and fold it into the open cycle."""
    filename = file.filename or "upload.csv"
    raw = await file.read()
    ws().auto_posted = None
    try:
        cycle = _ingest(raw, filename, reported)
    except SettlementParseError as exc:
        raise HTTPException(422, str(exc))
    d = cycle.digest()
    print(f"\n[Upload] {filename} → cycle {d['cycle']}, {d['open_exceptions']} open exception(s)")
    return {"filename": filename, "cycle": d["cycle"], "digest": d,
            "auto_post": getattr(ws(), "auto_posted", None)}


@app.post("/api/cycle/sample")
def api_load_sample():
    """Load the built-in sample cycle covering the three known edge cases."""
    space = ws()
    profile = space.profile()
    sample = Cycle(
        lines=sample_lines(), reported_payouts=dict(REPORTED_PAYOUTS),
        store=_rules(), prior_cycles=prior_cycle_lines(),
        firm=profile.firm, firm_id=space.id,
    )
    # A demonstration, kept out of the settlements list and out of storage: it
    # would otherwise sit among a firm's real months for ever.
    sample.sample = True
    space.cycle = sample
    space.cycle.run()
    return space.cycle.digest()


@app.delete("/api/cycle")
def api_clear_cycle():
    """Close the open cycle without posting. Rules already learned are kept."""
    space = ws()
    space.cycle = None
    # Closed on purpose, so it must not reappear at the next restart. The files
    # stay retained; only the open-cycle marker is cleared.
    profile = space.profile()
    if profile.open_cycle:
        profile.open_cycle = None
        _profiles.save(profile)
    return {"cycle": None}


class ReportedRequest(BaseModel):
    # Same shape as the upload field: "Shopee=3733.38;Lazada=1038.00"
    reported: str


@app.put("/api/cycle/reported")
def api_set_reported(req: ReportedRequest):
    """State what a platform actually deposited, after the file is already in.

    A settlement export does not always carry the payout on a row of its own,
    and the figure typed at upload is easy to leave blank. Without a way to
    supply it afterwards the only remedy was to close the cycle and upload
    again, losing every decision made since — so the whole payout sat as an
    unexplained residual that no approval could clear.
    """
    space = ws()
    cycle = _require_cycle()
    try:
        payouts = parse_reported(req.reported)
    except SettlementParseError as exc:
        raise HTTPException(400, str(exc))
    if not payouts:
        raise HTTPException(400, 'Nothing to set. Use the form "Shopee=3733.38".')

    known = {p.value for p in cycle.reported} | {
        l.platform.value for l in cycle.lines}
    unknown = [p.value for p in payouts if p.value not in known]
    if unknown:
        raise HTTPException(
            400, f"No lines in this cycle for: {', '.join(unknown)}.")

    cycle.reported.update(payouts)
    cycle.derived -= set(payouts)
    cycle.run()
    stated = ";".join(f"{p.value}={v:.2f}" for p, v in sorted(
        cycle.reported.items(), key=lambda kv: kv[0].value))
    cycle.trail.add("source", f"Reported payout set — {stated}",
                    actor=space.profile().approver())
    update_settlement_reported(space.id, cycle.cycle, stated)
    space.record(cycle)
    return {**cycle.digest(), "auto_post": _auto_post(cycle)}


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
    space = ws()
    cycle = _require_cycle()
    exc = _find_exception(cycle, req.key)
    # The firm's own decisions and the accounts actually in use. Without these
    # the model can describe a fee but never propose where it goes, which is the
    # only thing the accountant is in this conversation for.
    return {"message": reply(exc, req.history, cycle.lines, cycle.prior,
                             rules=space.rules().rules,
                             accounts=POSTING_ACCOUNTS)}


# Off in tests, so a proposal lands before the assertion that looks for it.
PROPOSE_IN_BACKGROUND = True


def _propose(cycle: Cycle, pending: list, rules: list) -> None:
    """Ask the model about exceptions it has not seen, and keep what it says.

    Takes everything it needs as arguments. It runs on its own thread after the
    upload has answered, where there is no signed-in request to ask for the
    workspace — and reconciling the month from here would race the requests
    still reading it.
    """
    try:
        out = triage(pending, cycle.lines, rules=rules, accounts=POSTING_ACCOUNTS)
        if out.get("error"):
            # Not marked as proposed: a model that was unreachable is asked
            # again next time, rather than never.
            cycle.proposal_error = out["error"]
            return
        cycle.proposal_error = ""
        for proposal in out.get("proposals", []):
            cycle.proposals[proposal["key"]] = proposal
        cycle.proposed.update(e.key for e in pending)
    finally:
        cycle.proposing = False


def _start_proposing(cycle: Optional[Cycle]) -> bool:
    """Propose treatments for a month's new exceptions, unless already under way.

    Nobody has to ask. The upload answers straight away and the proposals follow
    within seconds; the review screen shows them arriving. Nothing is approved:
    a proposal waits for the accountant exactly as it did behind the button.
    """
    if cycle is None or cycle.proposing:
        return False
    pending = [e for r in cycle.run().values() for e in r.exceptions
               if not e.resolved and e.key not in cycle.proposed]
    if not pending:
        return False
    cycle.proposing = True
    args = (cycle, pending, list(ws().rules().rules))
    if PROPOSE_IN_BACKGROUND:
        threading.Thread(target=_propose, args=args, daemon=True).start()
    else:
        _propose(*args)
    return True


@app.post("/api/exceptions/triage")
def api_triage(again: bool = False):
    """Fynn's proposals for every open exception, starting them if need be.

    Proposes only. Each one still has to be approved, and approving goes
    through the same path as deciding an exception by hand — so the same
    checks apply and the same rules get written.

    `again` asks afresh about exceptions already proposed, for when the
    accountant wants a second opinion rather than the cached one.
    """
    cycle = _require_cycle()
    if again and not cycle.proposing:
        cycle.proposed.clear()
        cycle.proposals.clear()
    _start_proposing(cycle)
    d = cycle.digest()
    return {"proposals": list(d["proposals"].values()), "proposing": d["proposing"],
            "error": d["proposal_error"]}


class BatchApproveRequest(BaseModel):
    # [{"key": ..., "account": ...}, ...]
    approvals: list[dict]
    actor: Optional[str] = None


@app.post("/api/approve/batch")
def api_approve_batch(req: BatchApproveRequest):
    """Approve several exceptions in one action.

    Each goes through the same approval as one decided by hand: the side comes
    from the amount's sign, a rule is written, and the decision is recorded
    against this cycle. Anything that cannot be applied is reported rather than
    skipped quietly — a batch that silently drops one is worse than a batch
    that fails.
    """
    space = ws()
    cycle = _require_cycle()
    actor = (req.actor or "").strip() or space.profile().approver()
    applied, failed = [], []
    for item in req.approvals:
        key, account = str(item.get("key", "")), str(item.get("account", ""))
        if account not in POSTING_ACCOUNTS:
            failed.append({"key": key, "why": f"{account!r} is not an account Fynn posts to."})
            continue
        try:
            exc = _find_exception(cycle, key)
        except HTTPException:
            failed.append({"key": key, "why": "No open exception with that key."})
            continue
        side = Side.CREDIT if exc.amount > 0 else Side.DEBIT
        cycle.approve(key, account, side, actor, True, "label")
        save_resolution(space.id, cycle.cycle, key, account, side.value, actor)
        applied.append({"key": key, "account": account})
    space.record(cycle)
    posted = _auto_post(cycle) if applied else None
    return {"applied": applied, "failed": failed, "digest": cycle.digest(),
            "auto_post": posted}


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
    # Rules cover labels that recur. This one is about this settlement — a
    # refund with no sale, a withheld balance — and without it a restart would
    # re-open a decision already made.
    save_resolution(ws().id, cycle.cycle, req.key, req.account, side.value, actor)
    ws().record(cycle)
    return {**cycle.digest(), "auto_post": _auto_post(cycle)}


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


# ── Settlements ───────────────────────────────────────────────────────────────
# The list a firm works from: every payout, every month, and where each one
# stands — in Fynn, and in Xero. See services/settlements.py.

def _find_settlement(reference: str):
    """A settlement's row, and the month and document behind it if Fynn has them."""
    space = ws()
    row = next((r for r in space.settlement_rows() if r["reference"] == reference), None)
    if row is None:
        raise HTTPException(404, f"No settlement {reference}.")
    cycle = space.month(row.get("cycle", ""))
    entry = result = None
    if cycle is not None:
        for e, r in settlements.documents(cycle, space.per_payout()):
            if e.reference == reference:
                entry, result = e, r
                break
    return row, cycle, entry, result


@app.get("/api/settlements")
def api_settlements(cycle: Optional[str] = None, platform: Optional[str] = None,
                    status: Optional[str] = None):
    """Every settlement, newest first, filtered as the list asks."""
    space = ws()
    rows = [settlements.present(r) for r in space.settlement_rows()]
    months = sorted({r["cycle"] for r in rows if r["cycle"]}, reverse=True)
    platforms = sorted({r["platform"] for r in rows if r["platform"]})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if cycle:
        rows = [r for r in rows if r["cycle"] == cycle]
    if platform:
        rows = [r for r in rows if r["platform"] == platform]
    if status:
        rows = [r for r in rows if r["status"] == status]
    profile = space.profile()
    return {
        "settlements": rows,
        "months": months,
        "platforms": platforms,
        "counts": counts,
        "labels": settlements.STATUS_LABELS,
        "ledger": profile.ledger,
        "connected": connections.get(space.id, "xero") is not None,
        "current": space.cycle.cycle if space.cycle is not None
                   and not getattr(space.cycle, "sample", False) else None,
    }


@app.post("/api/settlements/refresh")
def api_refresh_settlements():
    """Ask Xero where every document Fynn sent now stands.

    Only documents that can still move: a reconciled or voided one has
    finished, and asking about it again costs a call and says nothing new.
    """
    space = ws()
    connection = connections.get(space.id, "xero")
    rows = [r for r in space.settlement_rows()
            if r.get("posted_at") and r.get("ledger") == "xero"
            and (r.get("ledger_status") or "").upper() not in settlements.FINAL_IN_LEDGER]
    if not rows:
        return {"checked": 0, "changed": 0}
    if connection is None:
        return {"checked": 0, "changed": 0,
                "error": "Connect Xero in Settings to see what happened to what Fynn sent."}
    try:
        found = fetch_invoice_statuses(
            connection,
            ids=[r["ledger_id"] for r in rows if r.get("ledger_id")],
            numbers=[r["reference"] for r in rows if not r.get("ledger_id")],
        )
    except Exception as exc:
        return {"checked": 0, "changed": 0, "error": str(exc)}

    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    for row in rows:
        hit = found.get(row["reference"])
        if hit is None:
            continue
        update = {"reference": row["reference"], "cycle": row.get("cycle", ""),
                  "platform": row.get("platform", ""),
                  "ledger_status": hit["status"], "status_checked_at": now}
        if hit.get("id"):
            update["ledger_id"] = hit["id"]
        if hit.get("document"):
            update["document"] = hit["document"]
        if hit["status"] != (row.get("ledger_status") or "").upper():
            changed += 1
        save_settlement(space.id, update)
        sent = _posted_now.get((space.id, row["reference"]))
        if sent:
            sent.update({k: v for k, v in update.items()
                         if k in ("ledger_status", "ledger_id", "document")})
    return {"checked": len(rows), "changed": changed}


@app.get("/api/settlements/{reference}")
def api_settlement(reference: str):
    """One settlement, as A2X opens one: summary, fee lines, the document."""
    space = ws()
    row, cycle, entry, result = _find_settlement(reference)
    view = settlements.present(row)
    profile = space.profile()
    accounts = _accounts(profile.ledger)

    # What Xero holds, where Fynn may no longer change it; what it would send,
    # where it still may.
    frozen = bool(row.get("posted_at")) and not settlements.ledger_allows(row)[0]
    shown = row.get("posted_lines") if frozen else row.get("lines")
    document = [
        {**l, "code": getattr(accounts.get(l["account"]), "code", "") or "",
         "tax": getattr(accounts.get(l["account"]), "tax", "") or ""}
        for l in (shown or [])
    ]

    exceptions = []
    if cycle is not None and result is not None:
        exceptions = [{"key": e.key, "kind": e.kind, "amount": e.amount, "why": e.why}
                      for e in result.exceptions if not e.resolved]

    files = []
    if cycle is not None and entry is not None:
        files = sorted({l.source_ref for l in settlements.lines_of(cycle, entry)
                        if l.source_ref})
    return {
        **view,
        "document_lines": document,
        "document_frozen": frozen,
        "breakdown": (settlements.breakdown(cycle, entry, result, accounts)
                      if entry is not None else []),
        "line_count": (len(settlements.lines_of(cycle, entry))
                       if entry is not None else 0),
        "exceptions": exceptions,
        "files": files,
        "trail": [r for r in (cycle.trail.to_dicts() if cycle is not None else [])
                  if entry is None or r["kind"] != "post"
                  or reference in r["message"]],
        "available": entry is not None,
    }


@app.get("/api/settlements/{reference}/raw")
def api_settlement_raw(reference: str):
    """The settlement's own rows from the files it came from, as a download."""
    _row, cycle, entry, _result = _find_settlement(reference)
    if cycle is None or entry is None:
        raise HTTPException(404, "The files behind this settlement are not available.")
    return Response(
        content=settlements.raw_csv(settlements.lines_of(cycle, entry)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{reference.lower()}-raw.csv"'},
    )


@app.post("/api/settlements/{reference}/post")
def api_post_settlement(reference: str):
    """Send one settlement to the ledger — A2X's "Post to Xero" on a single row."""
    row, cycle, entry, _result = _find_settlement(reference)
    if cycle is None or entry is None:
        raise HTTPException(409, "Fynn cannot rebuild this settlement from its files.")
    sendable, why = settlements.can_send(row)
    if not sendable:
        raise HTTPException(409, why)
    replaces = {}
    if (row.get("ledger_status") or "").upper() in settlements.GONE_FROM_LEDGER:
        replaces[reference] = row.get("ledger_id") or "voided"
    out = _send(cycle, [entry], replaces)
    return {"adapter": get_adapter(_profile().ledger).name, "entries": out,
            "settlement": api_settlement(reference)}


def _deletion(reference: str) -> dict:
    """What deleting a settlement takes with it, and whether it may.

    A settlement is not stored on its own: it is what a file produces. So what
    is deleted is the upload behind it, and with it every settlement that upload
    made — one Lazada statement is four weekly settlements, and removing one
    week while its file stays would bring it back at the next rebuild.
    """
    space = ws()
    row, cycle, entry, _result = _find_settlement(reference)
    month = row.get("cycle", "")
    names: set[str] = set()
    refs = {reference}
    if cycle is not None and entry is not None:
        names = {l.source_ref for l in settlements.lines_of(cycle, entry) if l.source_ref}
        for other, _r in settlements.documents(cycle, space.per_payout()):
            if any(l.source_ref in names for l in settlements.lines_of(cycle, other)):
                refs.add(other.reference)
    try:
        from services.database import load_settlement_files
        files = [f for f in load_settlement_files(space.id, month)
                 if f.get("filename") in names]
    except Exception:
        files = []

    rows = [r for r in space.settlement_rows() if r["reference"] in refs]
    # In a client's ledger and still live there. A draft is still Fynn's, so
    # Delete takes it out of Xero too. Anything past a draft is not: deleting
    # Fynn's copy would leave a document nothing in Fynn accounts for, and
    # voiding it is the accountant's decision.
    live = [r for r in rows if r.get("posted_at")
            and r.get("ledger") not in ("", "dry-run")
            and (r.get("ledger_status") or "").upper() not in settlements.GONE_FROM_LEDGER]
    drafts = [r for r in live
              if (r.get("ledger_status") or "").upper() in settlements.EDITABLE_IN_LEDGER]
    in_ledger = [r for r in live if r not in drafts]
    if drafts and connections.get(space.id, "xero") is None:
        in_ledger = live
    return {
        "reference": reference,
        "cycle": month,
        "files": sorted(names) or [f.get("filename", "") for f in files],
        "file_ids": [f["id"] for f in files if f.get("id")],
        "settlements": [{"reference": r["reference"], "period": r.get("period", ""),
                         "status": settlements.present(r)["status_label"]}
                        for r in sorted(rows, key=settlements.sort_key)],
        "xero_drafts": [r["reference"] for r in drafts] if not in_ledger else [],
        "allowed": not in_ledger,
        "why_not": (
            "Xero is not connected, so Fynn cannot delete its drafts there. "
            "Reconnect Xero in Settings, or delete them in Xero first."
            if drafts and in_ledger == live and connections.get(space.id, "xero") is None
            else
            f"{len(in_ledger)} of these {'is' if len(in_ledger) == 1 else 'are'} already "
            f"approved or reconciled in Xero ({', '.join(r['reference'] for r in in_ledger)}). "
            f"Void {'it' if len(in_ledger) == 1 else 'them'} in Xero first, then open "
            "Settlements again so Fynn sees it, and delete here."
        ) if in_ledger else "",
        "_drafts": drafts,
    }


def _delete_xero_drafts(drafts: list[dict]) -> None:
    """Delete Fynn's drafts in Xero before Fynn forgets them.

    Xero first, and nothing in Fynn until every one is gone: the opposite order
    can leave a draft in a client's books that Fynn no longer lists.

    The status is asked again at the moment of deleting, not taken from the
    list. The list can be minutes old, and a draft approved in the meantime is
    no longer a draft to delete.
    """
    if not drafts:
        return
    space = ws()
    connection = connections.get(space.id, "xero")
    if connection is None:
        raise HTTPException(409, "Xero is not connected. Reconnect it in Settings.")
    try:
        found = fetch_invoice_statuses(
            connection,
            ids=[r["ledger_id"] for r in drafts if r.get("ledger_id")],
            numbers=[r["reference"] for r in drafts if not r.get("ledger_id")])
    except Exception as exc:
        raise HTTPException(502, f"Could not check the drafts in Xero: {exc}")

    moved = [r["reference"] for r in drafts
             if (found.get(r["reference"]) or {}).get("status") not in
             (None, *settlements.EDITABLE_IN_LEDGER, *settlements.GONE_FROM_LEDGER)]
    if moved:
        raise HTTPException(409, (
            f"{', '.join(moved)} {'was' if len(moved) == 1 else 'were'} approved in Xero "
            "since this list was loaded. Nothing was deleted. Void "
            f"{'it' if len(moved) == 1 else 'them'} in Xero first."))

    deleted = []
    for row in drafts:
        hit = found.get(row["reference"])
        if hit is None or hit["status"] in settlements.GONE_FROM_LEDGER:
            continue            # already gone from Xero; nothing to do there
        try:
            status = delete_draft_invoice(connection, hit["id"])
        except Exception as exc:
            done = f" {', '.join(deleted)} {'was' if len(deleted) == 1 else 'were'} " \
                   "deleted in Xero first." if deleted else ""
            raise HTTPException(502, (
                f"Xero did not delete {row['reference']}: {exc}.{done} Nothing was "
                "deleted in Fynn, so try again."))
        save_settlement(space.id, {"reference": row["reference"],
                                   "cycle": row.get("cycle", ""),
                                   "platform": row.get("platform", ""),
                                   "ledger_id": hit["id"], "ledger_status": status})
        deleted.append(row["reference"])


def _without_files(cycle: Cycle, names: set[str]) -> Optional[Cycle]:
    """The same month without some files' lines, or None if nothing is left."""
    keep = [l for l in cycle.lines if l.source_ref not in names]
    if not keep:
        return None
    touched = {l.platform for l in cycle.lines if l.source_ref in names}
    platforms = {l.platform for l in keep}
    # A platform the deleted files never touched keeps its payout as it was; one
    # they shared is left with what its remaining lines add up to.
    stated = {p: (round(sum(l.amount for l in keep if l.platform == p), 2)
                  if p in touched else cycle.stated.get(p, 0.0)) for p in platforms}
    typed = {p: v for p, v in cycle.reported.items()
             if p in platforms and p not in touched and p not in cycle.derived}
    left = Cycle(lines=keep, reported_payouts=typed, store=cycle.store,
                 prior_cycles=cycle.prior, cycle=cycle.cycle, firm=cycle.firm,
                 firm_id=cycle.firm_id, stated_totals=stated)
    left.resolutions = dict(cycle.resolutions)
    left.posted = [p for p in cycle.posted if any(
        l.platform.value == p.get("platform") for l in keep)]
    left.trail = cycle.trail
    left.run()
    return left


@app.get("/api/settlements/{reference}/delete")
def api_preview_delete(reference: str):
    """What Delete would remove, so the confirmation can say so before it happens."""
    plan = _deletion(reference)
    plan.pop("file_ids", None)
    plan.pop("_drafts", None)
    return plan


@app.delete("/api/settlements/{reference}")
def api_delete_settlement(reference: str):
    """Delete the upload behind a settlement, and every settlement it made.

    Rules the firm decided stay: they are about fee labels, not about this file,
    and the next upload will want them. A month left with no files loses its
    one-off decisions too, since they were about lines that are gone.
    """
    from services.database import (
        delete_dry_run_posts, delete_resolutions, delete_settlement,
        delete_settlement_file, load_settlement_files)
    space = ws()
    plan = _deletion(reference)
    if not plan["allowed"]:
        raise HTTPException(409, plan["why_not"])
    month = plan["cycle"]
    _delete_xero_drafts(plan["_drafts"])

    for file_id in plan["file_ids"]:
        delete_settlement_file(space.id, file_id)
    for item in plan["settlements"]:
        delete_settlement(space.id, item["reference"])
        delete_dry_run_posts(space.id, item["reference"])
        _posted_now.pop((space.id, item["reference"]), None)

    from services.database import _get_client
    if _get_client() is None and month in space._cycles:
        # Nothing stored to rebuild from, so take the deleted files' lines out
        # of the month in memory. Rebuilding from storage here would find no
        # files at all and delete every other platform's settlements with it.
        left = _without_files(space._cycles[month], set(plan["files"]))
        space.forget(month)
        if left is not None:
            space._cycles[month] = left
            space.record(left)
            return {"deleted_files": plan["files"],
                    "deleted_settlements": [s["reference"] for s in plan["settlements"]],
                    "deleted_in_xero": plan["xero_drafts"]}

    space.forget(month)
    remaining = load_settlement_files(space.id, month) if month else []
    if remaining:
        # Rebuilt from what is left, which also removes any settlement the
        # deleted file alone produced.
        space.month(month)
    else:
        if month:
            delete_resolutions(space.id, month)
        profile = space.profile()
        if space._current == month:
            space.cycle = None
        if profile.open_cycle == month:
            profile.open_cycle = None
            _profiles.save(profile)
    return {"deleted_files": plan["files"],
            "deleted_settlements": [s["reference"] for s in plan["settlements"]],
            "deleted_in_xero": plan["xero_drafts"]}


@app.post("/api/cycles/{month}/open")
def api_open_month(month: str):
    """Put a month on the review screen, without touching any other month."""
    cycle = ws().open_month(month)
    if cycle is None:
        raise HTTPException(404, f"No retained files for {month}.")
    # A month rebuilt after a restart has lost its proposals with the process.
    _start_proposing(cycle)
    return cycle.digest()


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
    connection = connections.get(ws().id, target)
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
        value = value or {}
        amap.set(account, value.get("code", ""), value.get("name", ""),
                 value.get("tax", ""))
    # A post held for an unmapped account is sent once the account is mapped,
    # or mapping it would quietly put the Post button back.
    return {**api_get_accounts(target), "auto_post": _auto_post(ws().cycle)}


OAUTH_STATE_COOKIE = "fynn_oauth_state"
OAUTH_SCOPE_COOKIE = "fynn_oauth_scopes"


@app.get("/oauth/{ledger}/connect", include_in_schema=False)
def oauth_connect(ledger: str):
    """Send the accountant to the ledger's consent screen."""
    try:
        provider = oauth.get_provider(ledger)
        # Random, single-use, and compared on the way back against a copy kept
        # in an httpOnly cookie: without it, a link from anywhere could complete
        # a connection into this workspace. The cookie is what makes it
        # trustworthy, so the value itself need only be unguessable — the same
        # pattern the Google sign-in uses, and it expires with the cookie.
        state = identity.new_state()
        url = oauth.authorize_url(provider, base_url(), state)
    except oauth.OAuthError as exc:
        raise HTTPException(400, str(exc))

    # Ask the provider before sending anyone there. If it will refuse, the
    # refusal renders on the provider's own error page with no way back — so
    # keep the accountant here and say what to fix.
    # Ask for the best set of permissions the app will actually grant, working
    # down. Xero is mid-way through replacing broad scopes with granular ones,
    # so the right names depend on when the firm's app was created — and a firm
    # whose app cannot request writing at all can still connect for reading,
    # which is enough to import the chart of accounts and finish the mapping.
    granted = provider.scopes
    refusal = oauth.preflight(provider, url)
    if refusal and "scope" in refusal.lower():
        for tier in provider.scope_tiers[1:]:
            attempt = oauth.authorize_url(provider, base_url(), state, scopes=tier)
            if oauth.preflight(provider, attempt) is None:
                url, refusal, granted = attempt, None, tier
                break

    if refusal:
        # The remedy has to match the refusal. Telling someone to check the
        # redirect URI when the scope is what was rejected sends them to look
        # at the one thing that is already correct.
        lower = refusal.lower()
        if "redirect" in lower:
            fix = (f"Add exactly this redirect URI to your {provider.label} app, then "
                   f"try again: {oauth.redirect_uri(base_url(), provider)}")
        elif "scope" in lower:
            fix = (f"Your {provider.label} app is not allowed to request any of the "
                   f"permission sets Fynn knows about. Enable one of these on the app, "
                   f"then try again: {' / '.join(provider.post_scopes)}, "
                   f"alongside accounting.settings.")
        else:
            fix = f"Check the app's configuration in {provider.label}, then try again."
        message = f"{provider.label} refused the request: {refusal}. {fix}"
        return RedirectResponse(f"/?ledger_error={quote(message, safe='')}", status_code=303)

    response = RedirectResponse(url, status_code=303)
    secure = bool(os.getenv("RAILWAY_PUBLIC_DOMAIN"))
    response.set_cookie(
        OAUTH_STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
        secure=secure,
    )
    # What we ended up asking for, so the callback records what the connection
    # can actually do rather than what the full request would have granted.
    response.set_cookie(
        OAUTH_SCOPE_COOKIE, granted, max_age=600, httponly=True, samesite="lax",
        secure=secure,
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
    if not state or not expected or not secrets.compare_digest(state, expected):
        return fail("That sign-in did not come from here, or it expired. Try again.")

    try:
        provider = oauth.get_provider(ledger)
        tokens = oauth.exchange_code(
            provider, code, base_url(), realm_id=realmId or None,
            requested_scopes=request.cookies.get(OAUTH_SCOPE_COOKIE, ""),
        )
    except oauth.OAuthError as exc:
        return fail(str(exc))

    try:
        connections.store(ws().id, provider.name, tokens)
    except Exception as exc:
        # Do not point the profile at a ledger we cannot reach tokens for.
        return fail(str(exc))

    # Point the firm at what they just connected, so posting goes there.
    profile = _profile()
    profile.ledger = provider.name
    _profiles.save(profile)

    readonly = "" if oauth.can_post(provider, tokens.get("scopes", "")) else "&readonly=1"
    response = RedirectResponse(
        f"/?connected={provider.name}{readonly}", status_code=303)
    response.delete_cookie(OAUTH_STATE_COOKIE)
    response.delete_cookie(OAUTH_SCOPE_COOKIE)
    return response


@app.post("/api/ledger/disconnect")
def api_disconnect(ledger: str):
    """Forget a ledger's tokens. The account mapping is kept."""
    connections.forget(ws().id, ledger)
    profile = _profile()
    if profile.ledger == ledger:
        profile.ledger = "dry-run"
        _profiles.save(profile)
    return connections.status(ws().id)


@app.get("/api/ledger/status")
def api_ledger_status():
    # Each ledger's redirect URI is returned in full rather than as a pattern
    # to fill in. Both providers match it byte-for-byte, so a URI retyped from
    # a template is the likeliest way a connection fails — and the error comes
    # back from the provider, after the redirect, where it is hard to read.
    status = connections.status(ws().id)
    for name, row in status.items():
        provider = oauth.get_provider(name)
        row["redirect_uri"] = oauth.redirect_uri(base_url(), provider)
        row["post_scopes"] = list(provider.post_scopes)
    return {"base_url": base_url(), "ledgers": status}


@app.get("/api/auth/status")
def api_auth_status():
    """How people can sign in, and what Google needs in order to allow it.

    The redirect URI is returned whole rather than as a pattern: Google matches
    it exactly and, like the ledgers, renders its refusal on its own error page
    where nothing here can explain it.
    """
    return {
        "google": {
            "configured": identity.google_configured(),
            "redirect_uri": identity.google_redirect_uri(base_url()),
        },
        "user": (current_user().to_dict() if current_user() else None),
    }


@app.get("/api/diagnostics")
def api_diagnostics():
    """Whether anything is actually being persisted.

    Every database write is deliberately best-effort, so a close keeps running
    through an outage — which also means a misconfiguration is invisible. This
    endpoint is how you tell the difference.
    """
    storage = diagnose()
    schema = migrate.status()
    # An account held only in memory is worse than a rule held only in memory:
    # a workspace is keyed by its owner's id, so losing the account orphans
    # everything under it. Say so separately and plainly.
    pending = _users.pending
    return {
        "storage": storage,
        "schema": schema,
        "accounts_at_risk": pending,
        "warning": (
            f"{pending} account(s) exist only in this process because the users "
            "table is missing. A restart loses them, and with them access to the "
            "workspace they own. Run the migrations now."
        ) if pending else (None if storage.get("connected") else (
            "Rules, settings, account mappings and retained files exist only in "
            "this process. A restart or redeploy loses them."
        )),
    }


@app.get("/api/rules")
def api_rules():
    store = _rules()
    return {"count": len(store.rules), "rules": store.export()}


@app.get("/api/audit.json")
def api_audit_json():
    return {"records": _require_cycle().trail.to_dicts()}


@app.get("/api/audit")
def api_audit_csv():
    """The working paper, as a file the browser saves rather than displays.

    Without the Content-Disposition header the browser renders CSV as text in
    the tab, which looks like the export failing — and leaves the app, since
    the link is a navigation.
    """
    cycle = _require_cycle()
    profile = _profile()
    paper = working_paper(cycle, _accounts(profile.ledger))
    stem = re.sub(r"[^A-Za-z0-9]+", "-", f"{profile.firm} {cycle.cycle}").strip("-").lower()
    return Response(
        content=paper,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="fynn-working-paper-{stem}.csv"'},
    )


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
    if req.auto_post is not None:
        profile.auto_post = req.auto_post
    # Saving settings no longer ends setup. Setup writes through this endpoint as
    # it goes — so that leaving for a ledger's consent screen does not lose what
    # was typed — and finishing it is a separate, deliberate act.
    _profiles.save(profile)
    if ws().cycle is not None:
        ws().cycle.firm = profile.firm
    return profile.to_dict()


@app.post("/api/onboarding/complete")
def api_onboarding_complete():
    """Setup is done, or was skipped. Either way, stop asking."""
    profile = _profile()
    profile.onboarding_step = None
    _profiles.save(profile)
    return profile.to_dict()


@app.post("/api/onboarding/restart")
def api_onboarding_restart():
    """Walk through setup again. Nothing is cleared — it opens on what is set."""
    profile = _profile()
    profile.onboarding_step = "ask_firm"
    _profiles.save(profile)
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
