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
    "firm_profiles": ["open_cycle"],
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
def a_clearing_line_is_only_ever_offered_a_current_asset():
    from models.account_map import suggest, clearing_type_ok
    chart = [{"code": "090", "name": "Shopee Clearing", "type": "CURRENT"},
             {"code": "200", "name": "Shopee Clearing Revenue", "type": "REVENUE"}]
    eq(suggest("Shopee Clearing Account", chart), "090", "suggested a non-asset")
    for kind in ("CURRENT", "BANK", "Other Current Asset", ""):
        ok(clearing_type_ok(kind), f"{kind} rejected")
    for kind in ("REVENUE", "EXPENSE", "CURRLIAB", "FIXED"):
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


@check("surface")
def no_route_returns_a_server_error():
    from fastapi.routing import APIRoute
    from tests.fakedb import FakeDB
    import main
    c = signup(client_for(FakeDB()), "sweep@firm.com")
    c.post("/api/cycle/sample")
    skip = {"/logout", "/auth/google", "/auth/google/callback", "/login", "/signup"}
    stand_in = {"ledger": "xero", "platform": "Shopee", "key": "nope"}
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


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    import os
    os.environ.setdefault("FYNN_SECRET", "test-secret")
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
