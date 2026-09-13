"""Schema, applied on startup.

Every table Fynn has ever needed arrived the same way: a feature was built, a
migration was written, and somebody had to notice, open the SQL editor and
paste it in. Until they did, the feature looked like it worked and silently
stored nothing — and once accounts existed, that stopped being a cosmetic
problem, because an account held only in memory takes its whole workspace with
it when the process restarts.

So the schema applies itself. Set DATABASE_URL and the app brings the database
up to date when it boots; nobody reads a migration file again.

Two properties make that safe to do on every boot:

  idempotent   Every statement here is `create table if not exists`, `create
               index if not exists` or `add column if not exists`. Running the
               whole set against an up-to-date database changes nothing and
               costs a few milliseconds.

  additive     Nothing drops, renames or rewrites. A migration that destroys
               data must never run unattended, so if one is ever needed it goes
               in a file that is deliberately not on this list.

DATABASE_URL is separate from SUPABASE_URL because it is a different thing: the
Postgres connection itself, with a password, rather than the PostgREST endpoint
and its service key. Without it this does nothing and says so — the app still
runs, and the diagnostics screen still reports whatever is missing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

# In order. Each is idempotent and additive; see the module docstring.
#
# 000 creates everything a fresh project needs. The two that follow it exist
# for databases created from an earlier copy of 000, where `create table if not
# exists` correctly skips a table that is already there — and so would never add
# a column to it.
APPLY = (
    "000_schema.sql",
    "010_connection_scopes.sql",
    "011_users.sql",
)


def describe(url: str) -> dict:
    """What the connection string actually says, minus the password.

    A rejected password is the least specific error Postgres has: the wrong
    password, the wrong username, and the right password on the wrong host all
    produce it. Showing the parsed fields ends the guessing — every one of them
    is either visible in the Supabase dashboard or obviously wrong, and none of
    them is a secret.

    The password itself is never returned. Whether it is present, and whether it
    holds a character that has to be percent-encoded, are.
    """
    from urllib.parse import urlsplit, unquote

    out = {"user": "", "host": "", "port": "", "database": "",
           "password_present": False, "password_needs_encoding": False,
           "password_is_placeholder": False, "is_pooler": False,
           "username_has_project_ref": False}
    if not url:
        return out

    # Checked against the raw string, before parsing: the square brackets in
    # [YOUR-PASSWORD] read as an IPv6 literal, so a URL still carrying the
    # placeholder does not parse at all and every field below would come back
    # blank — hiding the one fault that explains it.
    if "your-password" in url.lower() or "your_password" in url.lower():
        out["password_is_placeholder"] = True

    try:
        parts = urlsplit(url)
    except Exception:
        # Unparseable, and the placeholder above is much the likeliest reason.
        return out

    out["user"] = unquote(parts.username or "")
    out["host"] = parts.hostname or ""
    out["port"] = str(parts.port or "")
    out["database"] = (parts.path or "").lstrip("/")

    raw = parts.password or ""
    out["password_present"] = bool(raw)
    decoded = unquote(raw)
    out["password_is_placeholder"] = decoded.upper().strip() in (
        "[YOUR-PASSWORD]", "YOUR-PASSWORD", "[YOUR_PASSWORD]")
    # If these survive undecoded in the raw field, the string was assembled by
    # hand and the URL will be misread — often silently, by truncating.
    out["password_needs_encoding"] = any(c in raw for c in "@/?#[] ")

    out["is_pooler"] = "pooler.supabase.com" in out["host"]
    out["username_has_project_ref"] = "." in out["user"]
    return out


class Result:
    def __init__(self) -> None:
        self.configured = False
        self.ok = False
        self.applied: list[str] = []
        self.detail = ""
        self.connection: dict = {}

    def to_dict(self) -> dict:
        return {"configured": self.configured, "ok": self.ok,
                "applied": list(self.applied), "detail": self.detail,
                "connection": dict(self.connection)}


_last = Result()


def status() -> dict:
    return _last.to_dict()


def _url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


def _hint(error: str, url: str) -> str:
    """Turn a Postgres failure into the thing to actually go and change.

    These four account for essentially every first attempt. The driver's
    wording is accurate and tells you nothing about which field is wrong.
    """
    low = error.lower()

    if "password authentication failed" in low:
        # The pooler requires the project ref in the username; the direct
        # connection does not. Which one is in use decides the advice.
        user = ""
        try:
            user = url.split("://", 1)[1].split(":", 1)[0]
        except Exception:
            pass
        if "pooler.supabase.com" in url and "." not in user:
            return (f'Using the pooler, the username must be '
                    f'"postgres.<project-ref>", not "{user}". Copy the Session '
                    f'pooler string from Supabase → Connect exactly.')
        if "[your-password]" in low or "[your-password]" in url.lower():
            return ("The string still contains the literal [YOUR-PASSWORD] "
                    "placeholder. Replace it, square brackets included.")
        return ("The password in DATABASE_URL is wrong, or contains a character "
                "that has to be percent-encoded — @ : / ? # break a connection "
                "string. Reset the database password in Supabase and use one "
                "with only letters and digits.")

    if "tenant or user not found" in low:
        return ('The pooler username must be "postgres.<project-ref>". Copy the '
                "Session pooler string from Supabase → Connect exactly.")

    if "could not translate host name" in low or "name or service not known" in low:
        return "The host in DATABASE_URL is wrong. Re-copy it from Supabase."

    if "timeout" in low or "network is unreachable" in low or "no route" in low:
        return ("Nothing answered. The direct-connection host is IPv6-only and "
                "unreachable from many platforms — use the Session pooler "
                "string instead.")

    return ""


def apply() -> Result:
    """Bring the database up to date. Never raises; reports instead."""
    global _last
    result = Result()
    url = _url()
    result.configured = bool(url)
    result.connection = describe(url)

    if not url:
        result.detail = ("DATABASE_URL is not set, so the schema is not managed "
                         "here. Run the files in migrations/ by hand, or set it "
                         "and restart.")
        _last = result
        return result

    try:
        import psycopg
    except ImportError:
        result.detail = ("DATABASE_URL is set but psycopg is not installed. "
                         "Add psycopg[binary] to requirements.txt.")
        _last = result
        print(f"  [Schema] {result.detail}")
        _last = result
        return result

    try:
        # autocommit so each file lands on its own rather than one failure
        # rolling back the ones that already succeeded.
        with psycopg.connect(url, autocommit=True, connect_timeout=15) as conn:
            for name in APPLY:
                path = MIGRATIONS / name
                if not path.exists():
                    continue
                sql = path.read_text()
                with conn.cursor() as cur:
                    cur.execute(sql)
                result.applied.append(name)
        result.ok = True
        result.detail = f"Schema up to date ({len(result.applied)} file(s) applied)."
        print(f"  [Schema] {result.detail}")
    except Exception as exc:
        # A database that will not take the schema is worth shouting about, but
        # not worth refusing to start over: the app degrades to in-memory and
        # the diagnostics screen says exactly this.
        result.detail = (f"Could not apply the schema: {type(exc).__name__}: "
                         f"{exc}")[:400]
        hint = _hint(str(exc), url)
        if hint:
            result.detail += f"  — {hint}"
        print(f"  [Schema] {result.detail}")

    _last = result
    return result
