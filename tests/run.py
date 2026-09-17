"""Fynn's regression checks. `python3 tests/run.py`

No test framework, deliberately: this has to be runnable by anyone with the
repo and nothing installed, including in a deploy log.

What is here is what has actually broken. Every check below corresponds to a
real fault that reached a user, and the most important group is `schema drift`:
Fynn's schema grows faster than the database it is pointed at, and testing only
against an up-to-date one could not see the whole class.
"""
from __future__ import annotations

import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(group):
    def wrap(fn):
        CHECKS.append((group, fn.__name__.replace("_", " "), fn))
        return fn
    return wrap


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what or 'value'}: got {got!r}, wanted {want!r}")


def ok(cond, what):
    if not cond:
        raise AssertionError(what)


# ── helpers ───────────────────────────────────────────────────────────────────

def client_for(db):
    from fastapi.testclient import TestClient
    from tests.fakedb import install
    import main
    install(db)
    return TestClient(main.app)


def signup(c, email="a@firm.com"):
    r = c.post("/signup", data={"email": email, "password": "a long enough phrase"},
               follow_redirects=False)
    eq(r.status_code, 303, "signup")
    return c


def sample_file(name):
    return (ROOT / "data" / "sample_exports" / name).read_bytes()


def resolve_all(c, account="Other Expense", limit=120):
    for _ in range(limit):
        state = c.get("/api/state").json()
        cycle = state.get("cycle")
        if not cycle or not cycle["exceptions"]:
            return
        c.post("/api/approve", json={"key": cycle["exceptions"][0]["key"],
                                     "account": account, "save_rule": True,
                                     "scope": "label"})


def map_all(c):
    accounts = c.get("/api/accounts").json()["accounts"]
    c.put("/api/accounts", json={"mapping": {
        a["account"]: {"code": str(200 + i), "name": a["account"], "tax": ""}
        for i, a in enumerate(accounts)}})


# ── schema drift ──────────────────────────────────────────────────────────────
# The columns each migration adds. A write carrying one of these must degrade to
# writing the row without it, never to losing the row.
LATE_COLUMNS = {
    "firm_profiles": ["open_cycle", "auto_post"],
    "settlement_files": ["reported"],
    "ledger_connections": ["scopes"],
    "account_mappings": ["tax"],
}


@check("schema drift")
def a_new_account_works_on_a_database_missing_any_late_column():
    from tests.fakedb import FakeDB
    for table, columns in LATE_COLUMNS.items():
        for column in columns:
            db = FakeDB(missing_columns={table: [column]})
            c = signup(client_for(db), f"drift-{table}-{column}@firm.com")
            ok(db.count("firm_profiles") == 1,
               f"no workspace row when {table}.{column} is missing — "
               f"every foreign key fails after this")
            eq(c.get("/api/state").status_code, 200, f"state with {table}.{column} missing")


@check("schema drift")
def a_ledger_connection_survives_a_database_missing_late_columns():
    from tests.fakedb import FakeDB
    from services import connections
    for table, columns in LATE_COLUMNS.items():
        for column in columns:
            db = FakeDB(missing_columns={table: [column]})
            c = signup(client_for(db), f"conn-{table}-{column}@firm.com")
            firm = db.rows["firm_profiles"][0]["workspace_id"]
            connections.store(firm, "xero", {
                "access_token": "a", "refresh_token": "r",
                "expires_at": "2099-01-01T00:00:00+00:00", "scopes": "x"})
            ok(db.count("ledger_connections") == 1,
               f"ledger connection lost when {table}.{column} is missing")


@check("schema drift")
def a_missing_table_never_takes_the_request_with_it():
    from tests.fakedb import FakeDB
    for table in ("cycle_resolutions", "settlement_files", "posted_entries"):
        db = FakeDB(missing_tables=[table])
        c = signup(client_for(db), f"missing-{table}@firm.com")
        eq(c.post("/api/cycle/sample").status_code, 200, f"sample with {table} missing")
        resolve_all(c)
        eq(c.get("/api/state").status_code, 200, f"state with {table} missing")


# ── the journey a new account actually takes ──────────────────────────────────

@check("new account")
def signup_then_upload_then_resolve_then_post():
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "journey@firm.com")
    ok(db.count("firm_profiles") == 1, "signup created no workspace row")
    r = c.post("/api/upload",
               files={"file": ("shopee-income-statement-2026-01.csv",
                               sample_file("shopee-income-statement-2026-01.csv"), "text/csv")},
               data={"reported": "Shopee=3733.38"})
    eq(r.status_code, 200, "upload")
    resolve_all(c)
    eq(c.get("/api/state").json()["cycle"]["open_exceptions"], 0, "exceptions left open")
    map_all(c)
    eq(c.post("/api/post").status_code, 200, "post")


@check("new account")
def two_accounts_never_see_each_other():
    from tests.fakedb import FakeDB
    db = FakeDB()
    a = signup(client_for(db), "a@firm.com")
    a.post("/api/cycle/sample")
    a.put("/api/settings", json={"firm": "Firm A", "actor": "A",
                                 "platforms": ["Shopee"], "ledger": "dry-run"})
    from fastapi.testclient import TestClient
    import main
    b = signup(TestClient(main.app), "b@other.com")
    eq(b.get("/api/state").json()["cycle"], None, "B can see A's cycle")
    eq(b.get("/api/settings").json()["firm"], "Your firm", "B can see A's settings")
    eq(a.get("/api/settings").json()["firm"], "Firm A", "A lost its own settings")


@check("new account")
def the_same_google_identity_keeps_one_workspace_across_restarts():
    from tests.fakedb import FakeDB
    from models.user import UserStore
    import tests.fakedb as fake
    db = fake.install(FakeDB())

    def sign_in(store, sub, email):
        user = store.by_google(sub) or store.by_email(email)
        if user is None:
            user = store.create(email, google_sub=sub)
        return user

    first = sign_in(UserStore(), "sub-1", "n@firm.com").workspace_id
    for _ in range(3):
        again = sign_in(UserStore(), "sub-1", "n@firm.com").workspace_id
        eq(again, first, "workspace changed across a restart")
    eq(db.count("users"), 1, "a restart created a second account")


@check("new account")
def a_signed_out_browser_is_never_challenged_for_basic():
    """A challenge would raise the browser's own credential box over the app —
    and that box cannot set a session cookie, so it leads nowhere. Scripts,
    which send no Sec-Fetch-Mode, still get something they can act on."""
    from tests.fakedb import FakeDB
    c = client_for(FakeDB())

    page = c.get("/api/state", headers={"sec-fetch-mode": "cors"})
    eq(page.status_code, 401, "a signed-out fetch")
    ok("www-authenticate" not in page.headers, "a browser fetch was challenged")

    script = c.get("/api/state")
    eq(script.status_code, 401, "a signed-out script")
    ok("www-authenticate" in script.headers, "a script got no challenge to act on")

    # Credentials that failed are still told how to retry, browser or not.
    tried = c.get("/api/state", headers={"sec-fetch-mode": "cors",
                                         "authorization": "Basic bm86bm8="})
    ok("www-authenticate" in tried.headers, "a failed sign-in got no challenge")


# ── correctness of what gets posted ───────────────────────────────────────────

@check("posting")
def every_journal_balances_on_the_real_exports():
    from tests.fakedb import FakeDB
    for name, reported in (("shopee-income-statement-2026-01.csv", "Shopee=3733.38"),
                           ("lazada-account-statement-2026-01.csv", "Lazada=3194.59")):
        c = signup(client_for(FakeDB()), f"bal-{name}@firm.com")
        c.post("/api/upload", files={"file": (name, sample_file(name), "text/csv")},
               data={"reported": reported})
        resolve_all(c)
        for platform in c.get("/api/state").json()["cycle"]["platforms"]:
            j = c.get(f"/api/journal/{platform['platform']}").json()
            ok(j["balanced"], f"{name} {platform['platform']} does not balance")


@check("posting")
def payout_documents_sum_to_the_reported_payout():
    from decimal import Decimal
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "payout@firm.com")
    c.post("/api/upload",
           files={"file": ("lazada-account-statement-2026-01.csv",
                           sample_file("lazada-account-statement-2026-01.csv"), "text/csv")},
           data={"reported": "Lazada=3194.59"})
    resolve_all(c)
    map_all(c)
    entries = c.post("/api/post").json()["entries"]
    eq(len(entries), 4, "Lazada settles weekly: one document per payout")
    total = sum(Decimal(str(l["UnitAmount"]))
                for e in entries for l in e["document"]["Invoices"][0]["LineItems"])
    eq(total, Decimal("3194.59"), "documents do not sum to the payout")


@check("posting")
def a_suggestion_never_crosses_revenue_and_expense():
    """Shipping Income was offered Shipping Expense: an error that balances."""
    from models.account_map import suggest
    chart = [{"code": "425", "name": "Shipping Expense", "type": "EXPENSE"},
             {"code": "430", "name": "Shipping Income", "type": "OTHERINCOME"},
             {"code": "200", "name": "Sales Revenue", "type": "REVENUE"},
             {"code": "210", "name": "Sales Returns & Allowances", "type": "REVENUE"}]
    eq(suggest("Shipping Income", chart), "430", "income offered an expense account")
    eq(suggest("Shipping Expense", chart), "425", "expense offered an income account")
    eq(suggest("Sales Returns & Allowances", chart), "210", "contra account refused")
    without = [c for c in chart if c["code"] != "430"]
    eq(suggest("Shipping Income", without), None,
       "with no income account, it fell back to the expense one")


@check("posting")
def a_catch_all_is_only_ever_offered_another_catch_all():
    """Other Expense was offered Shipping Expense, quietly misfiling everything
    the firm could not classify."""
    from models.account_map import suggest
    named = [{"code": "425", "name": "Shipping Expense", "type": "EXPENSE"},
             {"code": "310", "name": "Commission Expense", "type": "EXPENSE"}]
    eq(suggest("Other Expense", named), None, "catch-all offered a named account")
    eq(suggest("Other Expense", named + [{"code": "800", "name": "Other Expenses",
                                          "type": "EXPENSE"}]), "800", "catch-all not matched")


@check("posting")
def xero_is_never_left_to_invent_the_tax():
    """An invoice came back with a subtotal of 0.00 and a total of 369.95 —
    every penny of it GST Xero applied from account defaults."""
    from models.account_map import AccountMap, LedgerAccount
    from models.transaction import JournalEntry, JournalLine, Platform, Side
    from services.ledger import XeroAdapter

    def build(rate):
        accounts = AccountMap("w", "xero", {
            "shopee clearing account": LedgerAccount("090"),
            "sales revenue": LedgerAccount("200", tax=rate),
            "commission expense": LedgerAccount("310")})
        entry = JournalEntry(reference="R", cycle="2026-01", platform=Platform.SHOPEE,
                             lines=[JournalLine(account="Shopee Clearing Account",
                                                side=Side.DEBIT, amount=900.0),
                                    JournalLine(account="Sales Revenue",
                                                side=Side.CREDIT, amount=1000.0),
                                    JournalLine(account="Commission Expense",
                                                side=Side.DEBIT, amount=100.0)])
        return XeroAdapter(accounts).build_payload(entry)["Invoices"][0]

    bare = build("")
    eq(bare["LineAmountTypes"], "NoTax", "Xero was left free to add tax")
    ok(all(l["TaxType"] == "NONE" for l in bare["LineItems"]),
       "a line without a mapped rate would fall back to the account default")

    mapped = build("OUTPUT2")
    eq(mapped["LineAmountTypes"], "Exclusive", "a mapped rate was not applied")
    rates = {l["AccountCode"]: l["TaxType"] for l in mapped["LineItems"]}
    eq(rates["200"], "OUTPUT2", "the mapped rate was not sent")
    eq(rates["310"], "NONE", "an unmapped account was left to its default")


@check("posting")
def the_payout_comes_from_the_file_without_being_typed():
    """Fynn read each row's stated total to check it, then threw the sum away
    and asked the accountant for the number it had just computed."""
    from services.csv_parser import parse_settlement_csv
    for name, expected in (("shopee-income-statement-2026-01.csv", 3733.38),
                           ("lazada-account-statement-2026-01.csv", 3194.59)):
        parsed = parse_settlement_csv(sample_file(name), name)
        total = round(sum(parsed.stated_totals.values()), 2)
        eq(total, expected, f"{name} did not state its own total")

    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "autopay@firm.com")
    c.post("/api/upload", files={"file": ("shopee-income-statement-2026-01.csv",
            sample_file("shopee-income-statement-2026-01.csv"), "text/csv")},
           data={"reported": ""})           # nothing typed
    eq(c.get("/api/state").json()["cycle"]["platforms"][0]["reported_payout"], 3733.38,
       "the payout was not taken from the file")

    # and a figure the firm supplies still wins, because only that one can come
    # from a bank statement
    c2 = signup(client_for(FakeDB()), "typed@firm.com")
    c2.post("/api/upload", files={"file": ("shopee-income-statement-2026-01.csv",
             sample_file("shopee-income-statement-2026-01.csv"), "text/csv")},
            data={"reported": "Shopee=3000.00"})
    eq(c2.get("/api/state").json()["cycle"]["platforms"][0]["reported_payout"], 3000.00,
       "a typed payout was overridden by the file")


@check("posting")
def a_posted_cycle_says_so():
    """After posting, the screen offered Post again with nothing to say it had
    already happened — so the only way to find out was to look in the ledger."""
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "posted@firm.com")
    c.post("/api/upload", files={"file": ("shopee-income-statement-2026-01.csv",
            sample_file("shopee-income-statement-2026-01.csv"), "text/csv")},
           data={"reported": "Shopee=3733.38"})
    resolve_all(c)
    map_all(c)
    eq(c.get("/api/state").json()["cycle"]["posted"], [], "posted before posting")
    eq(c.post("/api/post").status_code, 200, "post")
    posted = c.get("/api/state").json()["cycle"]["posted"]
    eq(len(posted), 1, "the post was not recorded")
    eq(posted[0]["reference"], "JE-SHO-2026-01", "the wrong reference was recorded")
    ok(posted[0]["at"], "no time recorded against the post")


LAZADA = "lazada-account-statement-2026-01.csv"
LAZADA_EXTRA = (b"Transaction Date,Transaction Type,Transaction Number,Order Number,"
                b"Order Item ID,Item Name,Comment,Amount,Statement Period\n"
                b"2026-01-10,Brand New Fee,MY199999,736057349,736057349-1,Cotton Tote Bag,,"
                b"-12.34,05 Jan 2026 - 11 Jan 2026\n")


def lazada_payout(c):
    p = next(p for p in c.get("/api/state").json()["cycle"]["platforms"]
             if p["platform"] == "Lazada")
    return p["reported_payout"], p["residual"]


@check("posting")
def a_redeploy_keeps_the_payout_read_from_the_file():
    """After a redeploy the Lazada payout came back as 0.00, reopening the whole
    deposit as an unexplained residual and holding the close."""
    import main as app
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "redeploy@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    resolve_all(c)
    before = lazada_payout(c)
    app._workspaces.clear()
    eq(lazada_payout(c), before, "payout after a redeploy")
    eq(c.get("/api/state").json()["cycle"]["open_exceptions"], 0, "exceptions reopened")


@check("posting")
def a_follow_up_file_adds_to_the_payout_instead_of_replacing_it():
    """A one-line file for the same platform became the month's payout: -12.34."""
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "followup@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    c.post("/api/upload", files={"file": ("lazada-extra.csv", LAZADA_EXTRA, "text/csv")})
    eq(lazada_payout(c)[0], 3182.25, "payout after a follow-up file")
    # The same file again adds nothing.
    c.post("/api/upload", files={"file": ("lazada-extra.csv", LAZADA_EXTRA, "text/csv")})
    eq(lazada_payout(c)[0], 3182.25, "payout after the same file twice")


@check("posting")
def a_residual_is_written_off_once_not_once_per_payout():
    from decimal import Decimal
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "residual-once@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")},
           data={"reported": "Lazada=3000.00"})
    resolve_all(c)
    map_all(c)
    entries = c.post("/api/post").json()["entries"]
    total = sum(Decimal(str(l["UnitAmount"]))
                for e in entries for l in e["document"]["Invoices"][0]["LineItems"])
    eq(total, Decimal("3000.00"), "documents do not sum to the payout")


@check("posting")
def a_corrected_entry_is_not_replayed_as_the_old_one():
    """A fee classified after the first post never reached Xero: the re-post
    carried the same idempotency key, so Xero answered with the original."""
    from models.account_map import AccountMap, LedgerAccount
    from models.transaction import JournalEntry, JournalLine, Platform, Side
    from services.ledger import XeroAdapter

    def entry(fee):
        return JournalEntry(reference="JE-LAZ-2026-01-X", cycle="2026-01",
                            platform=Platform.LAZADA, lines=[
            JournalLine(account="Lazada Clearing Account", side=Side.DEBIT, amount=100 - fee),
            JournalLine(account="Other Expense", side=Side.DEBIT, amount=fee),
            JournalLine(account="Sales Revenue", side=Side.CREDIT, amount=100)])

    adapter = XeroAdapter(AccountMap("w", "xero", {}))
    eq(adapter.idempotency_key(entry(5)), adapter.idempotency_key(entry(5)), "retry key")
    ok(adapter.idempotency_key(entry(5)) != adapter.idempotency_key(entry(7)),
       "a changed entry reuses the key of the one already sent")


@check("posting")
def an_entry_is_dated_in_the_period_it_belongs_to():
    """A January close was posted dated today, landing it in September."""
    from models.account_map import AccountMap, LedgerAccount
    from models.transaction import JournalEntry, JournalLine, Platform, Side
    from services.ledger import XeroAdapter, _cycle_end

    accounts = AccountMap("w", "xero", {"shopee clearing account": LedgerAccount("090"),
                                        "sales revenue": LedgerAccount("200")})

    def dated(cycle, payout):
        entry = JournalEntry(reference="R", cycle=cycle, platform=Platform.SHOPEE,
                             payout=payout,
                             lines=[JournalLine(account="Shopee Clearing Account",
                                                side=Side.DEBIT, amount=100.0),
                                    JournalLine(account="Sales Revenue",
                                                side=Side.CREDIT, amount=100.0)])
        return XeroAdapter(accounts).build_payload(entry)["Invoices"][0]["Date"]

    eq(dated("2026-01", "05 Jan 2026 - 11 Jan 2026"), "2026-01-11", "stated payout date")
    eq(dated("2026-01", "26 Jan 2026 - 01 Feb 2026"), "2026-02-01", "period crossing a month")
    eq(dated("2026-01", "2026-01"), "2026-01-31", "monthly statement dated outside its cycle")
    eq(_cycle_end("2024-02"), "2024-02-29", "leap year")
    eq(_cycle_end("2026-13"), "", "an impossible month should not be invented")


@check("posting")
def money_owed_to_the_firm_is_only_ever_offered_an_asset():
    """Clearing Account (hold) reached Retained Earnings, and Withholding Tax
    Receivable reached General Expenses. Neither was checked."""
    from models.account_map import clearing_type_ok, expects_asset, suggest
    chart = [{"code": "090", "name": "Shopee Clearing", "type": "CURRENT"},
             {"code": "200", "name": "Shopee Clearing Revenue", "type": "REVENUE"},
             {"code": "960", "name": "Retained Earnings", "type": "EQUITY"},
             {"code": "429", "name": "General Expenses", "type": "EXPENSE"}]
    eq(suggest("Shopee Clearing Account", chart), "090", "suggested a non-asset")
    eq(suggest("Clearing Account (hold)", chart), None, "offered equity or expense")
    eq(suggest("Withholding Tax Receivable", chart), None, "a receivable offered an expense")

    for name in ("Shopee Clearing Account", "Clearing Account (hold)",
                 "Withholding Tax Receivable"):
        ok(expects_asset(name), f"{name} is not held to being an asset")
    for name in ("Commission Expense", "Sales Revenue"):
        ok(not expects_asset(name), f"{name} should not be held to being an asset")

    for kind in ("CURRENT", "BANK", "Other Current Asset", "Accounts Receivable", ""):
        ok(clearing_type_ok(kind), f"{kind} rejected")
    for kind in ("REVENUE", "EXPENSE", "CURRLIAB", "FIXED", "EQUITY"):
        ok(not clearing_type_ok(kind), f"{kind} accepted")


# ── the whole surface ─────────────────────────────────────────────────────────

@check("posting")
def a_ledger_refusing_on_scope_is_explained_not_pasted():
    """Xero answers a missing scope with a bare 401 that reads as a broken login."""
    import httpx
    import services.ledger as ledger
    from models.account_map import AccountMap, LedgerAccount
    from models.transaction import JournalEntry, JournalLine, Platform, Side

    accounts = AccountMap("w", "xero", {"shopee clearing account": LedgerAccount("090"),
                                        "sales revenue": LedgerAccount("200")})
    entry = JournalEntry(reference="JE-SHO-2026-01", cycle="2026-01", platform=Platform.SHOPEE,
                         lines=[JournalLine(account="Shopee Clearing Account",
                                            side=Side.DEBIT, amount=100.0),
                                JournalLine(account="Sales Revenue",
                                            side=Side.CREDIT, amount=100.0)])

    class Connection:
        org_id, scopes, can_post = "t", "", True

        def access_token(self):
            return "tok"

    original = ledger.httpx.post
    ledger.httpx.post = lambda *a, **k: httpx.Response(
        401, json={"Title": "Unauthorized", "Detail": "AuthorizationUnsuccessful"},
        request=httpx.Request("POST", "https://api.xero.com"))
    try:
        ledger.XeroAdapter(accounts, Connection()).post(entry)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("a 401 was not raised as an error")
    finally:
        ledger.httpx.post = original
    ok("accounting.transactions" in message, "the message does not name the scope")
    ok("reconnect" in message.lower(), "the message does not say what to do")


@check("posting")
def the_best_scope_set_the_app_allows_is_the_one_requested():
    """Xero retired accounting.transactions for apps made after March 2026."""
    from services import oauth
    xero = oauth.PROVIDERS["xero"]
    ok(len(xero.scope_tiers) >= 2, "no fallback scope sets")
    ok("accounting.invoices" in xero.scope_tiers[0], "granular scopes are not preferred")
    ok(any("accounting.transactions" in t for t in xero.scope_tiers),
       "older apps using the broad scopes have no tier")
    ok(oauth.can_post(xero, xero.scope_tiers[0]), "granular tier cannot post")
    ok(oauth.can_post(xero, xero.scope_tiers[1]), "broad tier cannot post")
    read_only = [t for t in xero.scope_tiers if not oauth.can_post(xero, t)]
    ok(read_only, "no read-only tier to fall back to")
    for tier in xero.scope_tiers:
        ok("offline_access" in tier, f"no refresh token would come back for: {tier}")
        ok("accounting.settings" in tier,
           f"the chart of accounts could not be read with: {tier}")
        # Listed in the developer portal, answered access_denied when asked for,
        # and not needed. Including it denies the whole request.
        ok("app.connections" not in tier, f"app.connections is back in: {tier}")


@check("posting")
def a_refusal_on_our_own_redirect_is_not_mistaken_for_success():
    """access_denied comes back to Fynn's callback, not Xero's error page.
    Following redirects blindly swallowed it and the scope set looked fine."""
    import httpx
    from services import oauth
    provider = oauth.PROVIDERS["xero"]
    original = oauth.httpx.get
    hops = iter([
        httpx.Response(302, headers={"location":
            "https://example.test/oauth/xero/callback?error=access_denied&state=x"},
            request=httpx.Request("GET", "https://login.xero.com/x")),
    ])
    oauth.httpx.get = lambda *a, **k: next(hops)
    try:
        reason = oauth.preflight(provider, "https://login.xero.com/identity/connect/authorize?x=1")
    finally:
        oauth.httpx.get = original
    ok(reason and "access_denied" in reason, f"a denial was read as success: {reason!r}")


@check("posting")
def a_confidence_survives_whichever_scale_it_arrives_in():
    """A model asked for 0-100 sometimes answers 0.85. int(0.85) is 0, so every
    proposal came back untrusted and nothing was ever pre-selected."""
    from agents.triage import _confidence, _parse
    eq(_confidence(85), 85, "an integer confidence")
    eq(_confidence(0.85), 85, "a fractional confidence collapsed to nothing")
    eq(_confidence("0.85"), 85, "a fractional confidence as a string")
    eq(_confidence(1), 100, "1 read as one percent")
    for junk in (None, "high", "", [], 150, -5):
        ok(0 <= _confidence(junk) <= 100, f"{junk!r} escaped the range")

    # A reply cut off mid-object is still the proposals before it.
    eq(len(_parse('{"proposals": [{"key":"a","account":"X","confidence":9,"reason":"r"},'
                  '{"key":"b","account":"Y","confidence":8,"reason":"s"},'
                  '{"key":"c","acc')["proposals"]), 2, "a truncated reply lost everything")
    eq(_parse("I think you should..."), None, "prose was read as proposals")


@check("surface")
def no_route_returns_a_server_error():
    from fastapi.routing import APIRoute
    from tests.fakedb import FakeDB
    import main
    c = signup(client_for(FakeDB()), "sweep@firm.com")
    c.post("/api/cycle/sample")
    skip = {"/logout", "/auth/google", "/auth/google/callback", "/login", "/signup"}
    stand_in = {"ledger": "xero", "platform": "Shopee", "key": "nope",
                "reference": "JE-NOPE", "month": "2026-01"}
    broken = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute) or route.path in skip:
            continue
        path = route.path
        for name, value in stand_in.items():
            path = path.replace("{%s}" % name, value).replace("{%s:path}" % name, value)
        if "{" in path:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            try:
                code = c.request(method, path, follow_redirects=False).status_code
            except Exception as exc:
                broken.append(f"{method} {path} raised {type(exc).__name__}: {exc}")
                continue
            if code >= 500:
                broken.append(f"{method} {path} -> {code}")
    ok(not broken, "server errors: " + "; ".join(broken))


@check("surface")
def no_local_variable_shadows_a_top_level_function():
    """The Review-the-cycle button: `const go = ...` shadowed the router."""
    js = (ROOT / "static" / "app.html").read_text().split("<script>", 1)[1]
    names = set(re.findall(r"^(?:async\s+)?function\s+(\w+)", js, re.M))
    names |= set(re.findall(r"^const\s+(\w+)\s*=\s*(?:async\s*)?\(?[\w,\s]*\)?\s*=>", js, re.M))
    hits = [m.group(1) for m in re.finditer(r"^\s+(?:const|let|var)\s+(\w+)\s*=", js, re.M)
            if m.group(1) in names]
    ok(not hits, f"shadowed: {sorted(set(hits))}")


@check("surface")
def the_account_list_is_the_same_on_both_sides():
    """The explainer once refused an account the dropdown was offering."""
    from models.firm_profile import POSTING_ACCOUNTS
    js = (ROOT / "static" / "app.html").read_text()
    fallback = re.search(r"let ACCOUNTS=\[(.*?)\];", js, re.S).group(1)
    listed = [a.strip().strip('"') for a in fallback.replace("\n", "").split(",")]
    eq(listed, list(POSTING_ACCOUNTS), "UI fallback and server list differ")


@check("surface")
def every_migration_applied_on_boot_is_idempotent_and_additive():
    from services.migrate import APPLY, MIGRATIONS
    danger = re.compile(r"\b(drop|truncate|delete\s+from|alter\s+\w+\s+rename)\b", re.I)
    for name in APPLY:
        sql = (MIGRATIONS / name).read_text()
        body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
        for statement in [s.strip() for s in body.split(";") if s.strip()]:
            ok(not danger.search(statement), f"{name} is destructive: {statement[:60]}")
            ok(re.search(r"if\s+not\s+exists", statement, re.I),
               f"{name} is not idempotent: {statement[:60]}")


@check("surface")
def expected_tables_match_the_schema_file():
    from services.database import EXPECTED_TABLES
    schema = (ROOT / "migrations" / "000_schema.sql").read_text()
    created = set(re.findall(r"create table if not exists (\w+)", schema))
    missing = set(EXPECTED_TABLES) - created
    ok(not missing, f"000_schema.sql does not create: {sorted(missing)}")


# ── settlements: every payout, every month ────────────────────────────────────

LAZADA_FEB = (LAZADA_EXTRA.replace(b"2026-01-10", b"2026-02-10")
              .replace(b"05 Jan 2026 - 11 Jan 2026", b"02 Feb 2026 - 08 Feb 2026"))


def listed(c, **filters):
    query = "&".join(f"{k}={v}" for k, v in filters.items())
    return c.get("/api/settlements" + (f"?{query}" if query else "")).json()["settlements"]


@check("settlements")
def a_new_month_does_not_push_the_last_one_aside():
    """Uploading February replaced January on screen, and nothing listed it
    again — its files were kept, but there was no way back to them."""
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "months@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    c.post("/api/upload", files={"file": ("lazada-feb.csv", LAZADA_FEB, "text/csv")})
    rows = listed(c)
    eq(sorted({r["cycle"] for r in rows}), ["2026-01", "2026-02"], "months listed")
    eq(len(listed(c, cycle="2026-01")), 4, "January's four weekly payouts")
    ok(all(r["status"] == "needs_review" for r in listed(c, cycle="2026-01")),
       "January's open exceptions were forgotten when February arrived")
    eq(listed(c, cycle="2026-02")[0]["period"], "02 Feb 2026 - 08 Feb 2026",
       "a month of one payout should still name the platform's period")

    # And January can be picked up again exactly where it was left.
    eq(c.post("/api/cycles/2026-01/open").status_code, 200, "reopen January")
    resolve_all(c)
    ok(all(r["status"] == "ready" for r in listed(c, cycle="2026-01")), "January resolved")
    ok(all(r["status"] == "needs_review" for r in listed(c, cycle="2026-02")),
       "resolving January touched February")


@check("settlements")
def one_settlement_posts_on_its_own_and_the_rest_wait():
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "one@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    first = listed(c)[-1]["reference"]
    eq(c.post(f"/api/settlements/{first}/post").status_code, 409,
       "a settlement posted while its month had open exceptions")
    resolve_all(c)
    map_all(c)
    eq(c.post(f"/api/settlements/{first}/post").status_code, 200, "post one")
    statuses = {r["reference"]: r["status"] for r in listed(c)}
    eq(statuses.pop(first), "prepared", "the posted one")
    ok(set(statuses.values()) == {"ready"}, f"the others moved too: {statuses}")


@check("settlements")
def the_list_survives_a_redeploy_including_what_was_posted():
    import main as app
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "keeps@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    resolve_all(c)
    map_all(c)
    c.post("/api/post")
    before = {r["reference"]: (r["status"], r["total"]) for r in listed(c)}
    app._workspaces.clear()
    app._posted_now.clear()
    eq({r["reference"]: (r["status"], r["total"]) for r in listed(c)}, before,
       "settlements after a redeploy")
    detail = c.get(f"/api/settlements/{next(iter(before))}").json()
    ok(any(t["kind"] == "decision" for t in detail["trail"]),
       "the decisions behind a settlement vanished from its trail after a redeploy")


@check("settlements")
def posts_made_before_settlements_existed_are_not_offered_again():
    """January went to Xero before settlements were recorded. Listed as Ready
    to post, it would invite a second copy into the client's books."""
    import main as app
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "legacy@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    resolve_all(c)
    map_all(c)
    c.post("/api/post")
    db.rows["settlements"] = []           # as a database from before migration 015
    app._workspaces.clear()
    app._posted_now.clear()
    rows = listed(c)
    eq(len(rows), 4, "history did not appear")
    ok(all(r["posted_at"] for r in rows), "a posted document is offered as unposted")


@check("settlements")
def a_document_approved_in_xero_is_left_alone():
    from services.settlements import can_send, status_of
    sent = [{"account": "Lazada Clearing Account", "side": "debit", "amount": 90.0},
            {"account": "Sales Revenue", "side": "credit", "amount": 90.0}]
    row = {"reference": "R", "posted_at": "2026-02-01", "ledger": "xero",
           "lines": sent, "posted_lines": sent, "open_exceptions": 0}
    eq(status_of({**row, "ledger_status": "DRAFT"}), "draft", "draft")
    eq(status_of({**row, "ledger_status": "AUTHORISED"}), "approved", "approved")
    eq(status_of({**row, "ledger_status": "PAID"}), "reconciled", "matched to the deposit")
    # Gone from Xero: there is no ledger state left to report, so the row says
    # where Fynn stands — and keeps saying what the accountant has to do first.
    eq(status_of({**row, "ledger_status": "VOIDED"}), "ready", "voided")
    eq(status_of({**row, "ledger_status": "DELETED", "open_exceptions": 3}),
       "needs_review", "a voided settlement hid its month's open exceptions")
    changed = [dict(sent[0], amount=80.0), dict(sent[1], amount=80.0)]
    eq(status_of({**row, "ledger_status": "DRAFT", "lines": changed}), "changed",
       "a draft that no longer matches what Fynn would send")
    ok(can_send({**row, "ledger_status": "DRAFT", "lines": changed})[0], "a draft re-sends")
    for final in ("AUTHORISED", "PAID"):
        ok(not can_send({**row, "ledger_status": final, "lines": changed})[0],
           f"Fynn offered to overwrite a document {final} in Xero")
    ok(can_send({**row, "ledger_status": "VOIDED"})[0], "a voided document can go again")


@check("posting")
def approving_a_batch_reconciles_the_month_once():
    """Every approval re-ran the whole month — classifying every line and
    rebuilding every journal — and looking each exception up ran it again. On a
    month's worth of lines that is the difference between a button that
    responds and one that appears to hang. The answer must not change."""
    import main as app
    from tests.fakedb import FakeDB
    from utils.formatter import Cycle

    runs = []
    real = Cycle.run
    def counted(self):
        runs.append(1)
        return real(self)
    try:
        Cycle.run = counted
        c = signup(client_for(FakeDB()), "batchcost@firm.com")
        c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
        keys = [e["key"] for e in c.get("/api/state").json()["cycle"]["exceptions"]]
        runs.clear()
        r = c.post("/api/approve/batch", json={"approvals": [
            {"key": k, "account": "Other Expense"} for k in keys]}).json()
    finally:
        Cycle.run = real

    eq(len(r["applied"]), len(keys), "not every decision was applied")
    eq(r["failed"], [], "a decision in the batch failed")
    eq(r["digest"]["open_exceptions"], 0, "exceptions left open")
    ok(all(p["ties_out"] for p in r["digest"]["platforms"]), "the month no longer ties out")
    ok(len(runs) <= 3, f"{len(keys)} approvals reconciled the month {len(runs)} times")


@check("posting")
def a_batch_that_posts_nothing_still_says_what_happened():
    """Automatic posting reports only when it had something to send, so a month
    still holding an undecided exception answered the click with nothing at
    all. The page needs the count to say so."""
    from tests.fakedb import FakeDB
    c = signup(client_for(FakeDB()), "batchquiet@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    keys = [e["key"] for e in c.get("/api/state").json()["cycle"]["exceptions"]]
    r = c.post("/api/approve/batch", json={"approvals": [
        {"key": k, "account": "Other Expense"} for k in keys[:-3]]}).json()
    eq(len(r["applied"]), len(keys) - 3, "not every ticked decision was applied")
    eq(r["auto_post"], None, "setup: expected nothing to be sent")
    eq(r["digest"]["open_exceptions"], 3,
       "the page cannot say what is left without the count")


@check("settlements")
def every_platform_states_its_period_the_same_way():
    """Lazada states a weekly range and Shopee's monthly statement states none,
    so the list read as two systems side by side. One shape now — without
    inventing dates for a month that never stated any."""
    from services.settlements import period_label
    eq(period_label("26 Jan 2026 - 01 Feb 2026"), "26 Jan 2026 – 01 Feb 2026", "a range")
    eq(period_label("2026-01-05 to 2026-01-11"), "05 Jan 2026 – 11 Jan 2026", "ISO range")
    eq(period_label("", "2026-01"), "Jan 2026", "a month with no stated period")
    eq(period_label("2026-01", "2026-01"), "Jan 2026", "a period that is the month")
    eq(period_label("05 Jan 2026"), "05 Jan 2026", "a single date")
    eq(period_label("Week 2"), "Week 2", "wording Fynn cannot read")


@check("settlements")
def a_settlement_voided_in_xero_says_what_is_left_to_do():
    """Its document is gone from Xero, so the row describes Fynn's side again.
    Re-uploading writes only the computed half of the row, so the voided status
    outlived the document and hid both the open exceptions and the new figures."""
    from services.settlements import present
    sent = [{"account": "Lazada Clearing Account", "side": "debit", "amount": 90.0},
            {"account": "Sales Revenue", "side": "credit", "amount": 90.0}]
    row = {"reference": "R", "cycle": "2026-01", "posted_at": "2026-02-01",
           "ledger": "xero", "ledger_status": "VOIDED",
           "lines": sent, "posted_lines": sent, "open_exceptions": 0}

    gone = present(row)
    eq(gone["status_label"], "Ready to post", "a voided document is not a status")
    ok(gone["was_voided"], "the voided document was forgotten entirely")
    eq(gone["action"], "resend", "no way to send it again")

    reuploaded = present({**row, "lines": [dict(l, amount=80.0) for l in sent]})
    eq(reuploaded["status_label"], "Ready to post", "re-uploaded figures")
    blocked = present({**row, "open_exceptions": 3})
    eq(blocked["status_label"], "Needs review", "open exceptions stayed hidden")
    eq(blocked["action"], "review", "the month still has to be reviewed first")


@check("settlements")
def an_automatic_post_never_replaces_a_document_somebody_voided():
    """Removing a document in Xero is somebody's decision. Now that a voided
    settlement reads as Ready to post rather than Voided in Xero, automatic
    posting has to keep leaving it alone, or it takes that decision back."""
    import main as app
    from services import ledger
    from tests.fakedb import FakeDB

    class Connected:
        org_id, org_name, scopes, connected_at, can_post = "org", "Org", "", "", True
        def access_token(self): return "t"
        def to_dict(self): return {"ledger": "xero", "org_name": "Org"}

    sent = []
    def fake_post(self, entry):
        self._check(entry)
        self._resolve(entry)
        sent.append(entry.reference)
        return {"status": "posted", "adapter": "xero", "reference": entry.reference,
                "ledger_id": "id-" + entry.reference, "ledger_status": "DRAFT",
                "document": "ACCREC"}

    db = FakeDB()
    real = (ledger.XeroAdapter.post, app.connections.get)
    try:
        ledger.XeroAdapter.post = fake_post
        app.connections.get = lambda f, l: Connected()
        c = signup(client_for(db), "auto-voided@firm.com")
        c.put("/api/settings", json={"firm": "F", "ledger": "xero", "auto_post": True,
                                     "platforms": ["Lazada"]})
        c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
        resolve_all(c)
        map_all(c)          # the accounts exist once the exceptions are decided
        eq(len(sent), 4, "setup: the four weekly settlements were not sent")

        # Voided in Xero by hand, the way an accountant would.
        for row in db.rows["settlements"]:
            row["ledger_status"] = "VOIDED"
        app._posted_now.clear()
        rows = listed(c)
        ok(all(r["status"] == "ready" for r in rows), "a voided settlement is not ready to post")
        ok(all(r["was_voided"] for r in rows), "the void was forgotten")

        sent.clear()
        accounts = c.get("/api/accounts").json()["accounts"]
        c.put("/api/accounts", json={"mapping": {
            a["account"]: {"code": str(400 + i), "name": a["account"], "tax": ""}
            for i, a in enumerate(accounts)}})
        eq(sent, [], "automatic posting sent a document somebody had voided")
    finally:
        ledger.XeroAdapter.post, app.connections.get = real


@check("settlements")
def xero_status_comes_back_into_the_list():
    import main as app
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "status@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    resolve_all(c)
    map_all(c)
    c.post("/api/post")
    refs = [r["reference"] for r in listed(c)]
    for row in db.rows["settlements"]:     # as if they had gone to Xero
        row.update({"ledger": "xero", "ledger_status": "DRAFT", "ledger_id": ""})
    app._posted_now.clear()

    asked = {}
    real_fetch, real_get = app.fetch_invoice_statuses, app.connections.get
    try:
        app.connections.get = lambda firm, ledger: object()
        def fake(connection, ids=(), numbers=()):
            asked["numbers"] = list(numbers)
            return {refs[0]: {"id": "inv-1", "status": "PAID", "document": "ACCREC"}}
        app.fetch_invoice_statuses = fake
        r = c.post("/api/settlements/refresh").json()
    finally:
        app.fetch_invoice_statuses, app.connections.get = real_fetch, real_get
    eq(sorted(asked["numbers"]), sorted(refs), "documents without an id looked up by number")
    eq(r["changed"], 1, "changed count")
    row = next(r for r in listed(c) if r["reference"] == refs[0])
    eq(row["status"], "reconciled", "a paid invoice")
    ok("inv-1" in row["ledger_url"], "no link into Xero once the id is known")
    eq(c.post(f"/api/settlements/{refs[0]}/post").status_code, 409,
       "a reconciled document was sent again")


@check("settlements")
def the_sample_never_enters_a_firms_settlements():
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "sample-list@firm.com")
    c.post("/api/cycle/sample")
    eq(listed(c), [], "the sample was listed as a real settlement")
    eq(db.count("settlements"), 0, "the sample was stored")


@check("settlements")
def a_refund_is_traced_to_a_sale_in_an_earlier_month():
    """A February refund for a January order said only "no matching sale" —
    January was never looked in, though its file was retained."""
    from tests.fakedb import FakeDB
    header = (b"Transaction Date,Transaction Type,Transaction Number,Order Number,"
              b"Order Item ID,Item Name,Comment,Amount,Statement Period\n")
    jan = header + (b"2026-01-10,Item Price Credit,T1,900001,900001-1,Tote,,50.00,"
                    b"05 Jan 2026 - 11 Jan 2026\n")
    feb = header + (b"2026-02-03,Reversal Item Price,T2,900001,900001-1,Tote,,-50.00,"
                    b"02 Feb 2026 - 08 Feb 2026\n")
    c = signup(client_for(FakeDB()), "refund@firm.com")
    c.post("/api/upload", files={"file": ("lazada-jan.csv", jan, "text/csv")})
    c.post("/api/upload", files={"file": ("lazada-feb.csv", feb, "text/csv")})
    refund = next(e for e in c.get("/api/state").json()["cycle"]["exceptions"]
                  if e["kind"] in ("orphan_refund", "partial_refund"))
    ok(refund["evidence"] and "2026-01" in refund["evidence"]["summary"],
       f"January's sale was not found: {refund['evidence']}")



@check("settlements")
def the_same_file_twice_is_refused_under_any_name():
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "twice@firm.com")
    eq(c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")}
              ).status_code, 200, "first upload")
    for name in (LAZADA, "lazada-account-statement-2026-01 (1).csv"):
        r = c.post("/api/upload", files={"file": (name, sample_file(LAZADA), "text/csv")})
        eq(r.status_code, 409, f"the same file again as {name}")
    eq(db.count("settlement_files"), 1, "a second copy was stored")
    eq(len(listed(c)), 4, "settlements after the repeats")


@check("settlements")
def posts_are_shown_as_sent_even_when_the_table_came_later():
    """The settlements table was created after the app had loaded January, so
    January's posts were never copied across and every row read Ready to post."""
    import main as app
    from tests.fakedb import FakeDB
    db = FakeDB(missing_tables=["settlements"])
    c = signup(client_for(db), "late-table@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    resolve_all(c)
    map_all(c)
    c.post("/api/post")
    app._posted_now.clear()
    ok(all(r["posted_at"] for r in listed(c)), "posted while the table was missing")
    db.missing_tables.clear()               # the table is created by hand
    ok(all(r["posted_at"] for r in listed(c)), "posted once the table exists")
    ok(all(r.get("posted_at") for r in db.rows.get("settlements", [])),
       "what was sent was not written into the new table")


@check("settlements")
def deleting_a_settlement_removes_its_upload_and_everything_it_made():
    import main as app
    from tests.fakedb import FakeDB
    db = FakeDB()
    c = signup(client_for(db), "delete@firm.com")
    c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
    c.post("/api/upload", files={"file": ("shopee-income-statement-2026-01.csv",
            sample_file("shopee-income-statement-2026-01.csv"), "text/csv")})
    week = next(r["reference"] for r in listed(c) if r["platform"] == "Lazada")
    plan = c.get(f"/api/settlements/{week}/delete").json()
    eq(len(plan["settlements"]), 4, "one Lazada file is four weekly settlements")
    eq(plan["files"], [LAZADA], "the file named in the confirmation")
    eq(c.delete(f"/api/settlements/{week}").status_code, 200, "delete")
    eq([r["platform"] for r in listed(c)], ["Shopee"], "what is left")
    eq(db.count("settlement_files"), 1, "the Lazada file was kept")
    app._workspaces.clear()
    eq([r["platform"] for r in listed(c)], ["Shopee"], "Lazada came back after a restart")
    eq(c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")}
              ).status_code, 200, "the deleted file could not be uploaded again")


@check("settlements")
def delete_takes_drafts_out_of_xero_and_leaves_approved_ones_alone():
    import main as app
    from tests.fakedb import FakeDB

    def posted_to_xero(email):
        db = FakeDB()
        c = signup(client_for(db), email)
        c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
        resolve_all(c)
        map_all(c)
        c.post("/api/post")
        for row in db.rows["settlements"]:
            row.update({"ledger": "xero", "ledger_status": "DRAFT",
                        "ledger_id": "id-" + row["reference"]})
        app._posted_now.clear()
        return db, c, listed(c)[0]["reference"]

    xero = {}                                   # invoice id -> status, as Xero holds it
    real = (app.fetch_invoice_statuses, app.delete_draft_invoice, app.connections.get)

    def fetch(connection, ids=(), numbers=()):
        return {i[3:]: {"id": i, "status": xero[i], "document": "ACCREC"} for i in ids}

    def delete(connection, invoice_id):
        if xero.get("fail") == invoice_id:
            raise RuntimeError("boom")
        xero[invoice_id] = "DELETED"
        return "DELETED"

    try:
        app.fetch_invoice_statuses, app.delete_draft_invoice = fetch, delete

        # Not connected: nothing can be deleted in Xero, so nothing is deleted.
        app.connections.get = lambda firm, ledger: None
        db, c, week = posted_to_xero("xero-off@firm.com")
        eq(c.delete(f"/api/settlements/{week}").status_code, 409, "deleted with Xero disconnected")

        class Connected:
            org_id, org_name, scopes, connected_at, can_post = "org", "Org", "", "", True
            def access_token(self): return "t"
            def to_dict(self): return {"ledger": "xero", "org_name": "Org"}
        app.connections.get = lambda firm, ledger: Connected()

        # Drafts: gone from Xero and from Fynn in one click.
        db, c, week = posted_to_xero("xero-drafts@firm.com")
        xero.clear()
        xero.update({"id-" + r["reference"]: "DRAFT" for r in db.rows["settlements"]})
        plan = c.get(f"/api/settlements/{week}/delete").json()
        eq(len(plan["xero_drafts"]), 4, "drafts named in the confirmation")
        r = c.delete(f"/api/settlements/{week}")
        eq(r.status_code, 200, "delete")
        eq(sorted(xero.values()), ["DELETED"] * 4, "the drafts were left in Xero")
        eq(listed(c), [], "the settlements were left in Fynn")

        # Approved in Xero since the list was drawn: nothing happens anywhere.
        db, c, week = posted_to_xero("xero-raced@firm.com")
        xero.clear()
        xero.update({"id-" + r["reference"]: "DRAFT" for r in db.rows["settlements"]})
        xero["id-" + week] = "AUTHORISED"
        eq(c.delete(f"/api/settlements/{week}").status_code, 409, "an approved invoice")
        ok("AUTHORISED" not in [v for k, v in xero.items() if k != "id-" + week]
           and "DELETED" not in xero.values(), "a draft was deleted before the refusal")
        eq(len(listed(c)), 4, "Fynn deleted its settlements anyway")

        # Xero fails part-way: Fynn keeps everything, so the delete can be retried.
        xero.update({k: "DRAFT" for k in xero})
        xero["fail"] = "id-" + listed(c)[-1]["reference"]
        eq(c.delete(f"/api/settlements/{week}").status_code, 502, "a failure in Xero")
        eq(len(listed(c)), 4, "Fynn forgot settlements Xero still holds")
        eq(db.count("settlement_files"), 1, "the file went although Xero refused")
    finally:
        app.fetch_invoice_statuses, app.delete_draft_invoice, app.connections.get = real

    # Already approved when the list was drawn: refused before anything is sent.
    from services.settlements import ledger_allows
    ok(not ledger_allows({"posted_at": "x", "ledger": "xero", "ledger_status": "PAID"})[0],
       "a reconciled invoice")


@check("settlements")
def a_resolved_month_posts_itself_only_when_the_firm_asked():
    import main as app
    from services import ledger
    from tests.fakedb import FakeDB

    class Connected:
        org_id, org_name, scopes, connected_at, can_post = "org", "Org", "", "", True
        def access_token(self): return "t"
        def to_dict(self): return {"ledger": "xero", "org_name": "Org"}

    sent = []
    def fake_post(self, entry):
        self._check(entry)
        self._resolve(entry)
        sent.append(entry.reference)
        return {"status": "posted", "adapter": "xero", "reference": entry.reference,
                "ledger_id": "id-" + entry.reference, "ledger_status": "DRAFT",
                "document": "ACCREC"}

    def firm(email, auto, mapped=True):
        c = signup(client_for(FakeDB()), email)
        c.put("/api/settings", json={"firm": "F", "ledger": "xero", "auto_post": auto,
                                     "platforms": ["Lazada"]})
        c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
        if mapped:
            map_all(c)
        return c

    real = (ledger.XeroAdapter.post, app.connections.get)
    try:
        ledger.XeroAdapter.post = fake_post
        app.connections.get = lambda f, l: Connected()

        c = firm("auto-off@firm.com", False)
        resolve_all(c)
        eq(sent, [], "posted without the setting on")

        c = firm("auto-on@firm.com", True, mapped=False)
        eq(sent, [], "posted while exceptions were still open")
        keys = [e["key"] for e in c.get("/api/state").json()["cycle"]["exceptions"]]
        for key in keys[:-1]:
            c.post("/api/approve", json={"key": key, "account": "Other Expense"})
        eq(sent, [], "posted before the last exception")
        map_all(c)
        r = c.post("/api/approve", json={"key": keys[-1], "account": "Other Expense"}).json()
        eq(len(sent), 4, "the four weekly settlements after the last decision")
        eq(len(r["auto_post"]["sent"]), 4, "the approval did not say what was sent")
        ok(all(x["status"] == "draft" for x in listed(c)), "not shown as drafts in Xero")

        # Settings saved again by setup, which does not send auto_post, keep it on.
        c.put("/api/settings", json={"firm": "F", "ledger": "xero", "platforms": ["Lazada"]})
        ok(c.get("/api/settings").json()["auto_post"], "setup switched automatic posting off")

        sent.clear()
        c = firm("auto-unmapped@firm.com", True, mapped=False)
        keys = [e["key"] for e in c.get("/api/state").json()["cycle"]["exceptions"]]
        for key in keys:
            last = c.post("/api/approve", json={"key": key, "account": "Other Expense"})
        eq(last.status_code, 200, "an approval failed because the post could not go")
        ok("not mapped" in last.json()["auto_post"]["error"], "no reason given for holding it")
        eq(sent, [], "an unmapped entry was sent")
        ok(all(x["status"] == "ready" for x in listed(c)), "held settlements not left ready")
        accounts = c.get("/api/accounts").json()["accounts"]
        r = c.put("/api/accounts", json={"mapping": {
            a["account"]: {"code": str(300 + i), "name": a["account"], "tax": ""}
            for i, a in enumerate(accounts)}}).json()
        eq(len(r["auto_post"]["sent"]), 4, "mapping the account did not send what was held")
    finally:
        ledger.XeroAdapter.post, app.connections.get = real



@check("settlements")
def proposals_arrive_after_an_upload_without_asking():
    """Suggestions waited behind a Propose button, after the accountant had
    already waited for the reconciliation."""
    import main as app
    from tests.fakedb import FakeDB
    asked = []

    def fake_triage(exceptions, lines, rules=None, accounts=None):
        asked.append(len(exceptions))
        if fake_triage.down:
            return {"proposals": [], "error": "Could not reach the model (Timeout)."}
        return {"error": "", "proposals": [
            {"key": e.key, "account": "Other Expense", "confidence": 90,
             "reason": "r", "trusted": True} for e in exceptions]}
    fake_triage.down = False

    real = app.triage
    try:
        app.triage = fake_triage
        c = signup(client_for(FakeDB()), "proposals@firm.com")
        c.post("/api/upload", files={"file": (LAZADA, sample_file(LAZADA), "text/csv")})
        cycle = c.get("/api/state").json()["cycle"]
        eq(len(asked), 1, "the model was not asked after the upload")
        eq(len(cycle["proposals"]), cycle["open_exceptions"], "proposals on the review screen")
        eq(cycle["unproposed"], 0, "exceptions left unproposed")

        c.post("/api/exceptions/triage")
        c.post("/api/cycles/2026-01/open")
        eq(len(asked), 1, "the same exceptions were put to the model again")

        c.post("/api/upload", files={"file": ("lazada-extra.csv", LAZADA_EXTRA, "text/csv")})
        eq(asked[-1], 1, "a follow-up file should ask only about its new exception")

        # An unreachable model is asked again next time, not never.
        fake_triage.down = True
        eq(c.post("/api/exceptions/triage?again=true").json()["error"] != "", True,
           "the failure was not reported")
        ok(c.get("/api/state").json()["cycle"]["unproposed"] > 0,
           "a failed attempt marked the exceptions as proposed")
        fake_triage.down = False
        c.post("/api/exceptions/triage")
        eq(c.get("/api/state").json()["cycle"]["unproposed"], 0, "no retry after a failure")
    finally:
        app.triage = real


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    import os
    os.environ.setdefault("FYNN_SECRET", "test-secret")
    # Proposals now start on every upload. A developer's .env holds a real key,
    # and without this every run of the suite would call the live model.
    os.environ["ANTHROPIC_API_KEY"] = ""
    import main as app
    app.PROPOSE_IN_BACKGROUND = False
    passed = failed = 0
    group = None
    for name, title, fn in CHECKS:
        if name != group:
            group = name
            print(f"\n{name.upper()}")
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {title}")
            for line in traceback.format_exception_only(type(exc), exc):
                print(f"          {line.rstrip()}")
        else:
            passed += 1
            print(f"  ok    {title}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
