"""Supabase persistence for Fynn.

Tables (migrations/005_reconciliation.sql, then 007):
  firm_profiles      — one row per accounting firm, keyed by workspace id
  firm_rules         — the accumulating asset: every decision a firm has made
  settlement_files   — source files, retained for the statutory period
  posted_entries     — journals sent to a ledger, with their audit trail

Designed with a graceful fallback — if Supabase is not configured every
operation silently no-ops and the app runs on in-process state. Local dev needs
zero Supabase setup, and a storage outage degrades Fynn to a single-cycle tool
rather than taking it down.
"""

import json
import os
from typing import Optional

_client = None


def _get_client():
    """Lazily initialise the Supabase client. None when not configured."""
    global _client
    if _client is not None:
        return _client

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        print("  [DB] Supabase connected.")
        return _client
    except Exception as exc:
        print(f"  [DB] Supabase connection failed: {exc}")
        return None


def _upsert(table: str, row: dict, optional: tuple = (), on_conflict: str = "") -> bool:
    """Upsert a row, tolerating a database that is a migration behind.

    Fynn's schema grows, and the database it is pointed at does not always grow
    with it — a column added last week may not exist yet. Without this, a write
    that carries a new field fails whole, and the damage is rarely local: a
    firm_profiles row that never lands takes every foreign key with it, so
    rules, settlement files and ledger connections all fail too, each for a
    reason that looks nothing like the cause.

    So the newest fields are named as optional. If the database rejects one,
    it is dropped and the row is written without it. Losing a field degrades a
    feature; losing the row breaks the account.
    """
    client = _get_client()
    if not client:
        return False

    def write(payload: dict) -> None:
        query = client.table(table).upsert(
            payload, **({"on_conflict": on_conflict} if on_conflict else {}))
        query.execute()

    try:
        write(row)
        return True
    except Exception as exc:
        missing = [k for k in optional if k in str(exc)]
        if not missing:
            print(f"  [DB] Failed to write {table}: {exc}")
            return False
        try:
            trimmed = {k: v for k, v in row.items() if k not in missing}
            write(trimmed)
            print(f"  [DB] Wrote {table} without {', '.join(missing)} — "
                  f"the database is behind; run the migrations.")
            return True
        except Exception as retry_exc:
            print(f"  [DB] Failed to write {table}: {retry_exc}")
            return False


# ── Firm profiles ─────────────────────────────────────────────────────────────

def save_firm(profile) -> bool:
    """Upsert one firm profile."""
    ok = _upsert("firm_profiles", {
        "workspace_id":    profile.id,
        "firm":            profile.firm,
        "actor":           profile.actor,
        "platforms":       profile.platforms,
        "ledger":          profile.ledger,
        "onboarding_step": profile.onboarding_step,
        "open_cycle":      profile.open_cycle,
    }, optional=("open_cycle",))
    if ok:
        print(f"  [DB] Firm profile saved for {profile.id}")
    return ok


def load_firm(firm_id: str) -> Optional[dict]:
    client = _get_client()
    if not client:
        return None
    try:
        result = (client.table("firm_profiles").select("*")
                  .eq("workspace_id", firm_id).execute())
        if not result.data:
            return None
        row = dict(result.data[0])
        row["id"] = row.get("workspace_id")
        return row
    except Exception as exc:
        print(f"  [DB] Failed to load firm profile: {exc}")
        return None


def load_all_firms() -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return client.table("firm_profiles").select("*").execute().data or []
    except Exception as exc:
        print(f"  [DB] Failed to load firm profiles: {exc}")
        return []


# ── Firm rules ────────────────────────────────────────────────────────────────

def save_rule(firm_id: str, rule) -> bool:
    """Persist one learned rule.

    Upserted on (firm, platform, label) so re-deciding a label overwrites the
    firm's earlier treatment rather than leaving two rules that disagree. A
    firm-wide rule is stored with platform '*' — a null there would be distinct
    from every other null and let duplicates accumulate.
    """
    client = _get_client()
    if not client:
        return False
    try:
        client.table("firm_rules").upsert(
            {
                "firm_id":    firm_id,
                "platform":   rule.platform.value if rule.platform else "*",
                "label":      rule.label,
                "account":    rule.account,
                "side":       rule.side.value,
                "decided_by": rule.decided_by,
                "decided_at": rule.decided_at.isoformat() if rule.decided_at else None,
            },
            on_conflict="firm_id,platform,label",
        ).execute()
        print(f'  [DB] Rule saved for {firm_id}: "{rule.label}" → {rule.account}')
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save rule: {exc}")
        return False


def load_rules(firm_id: str) -> list[dict]:
    """Return a firm's learned rules, shaped for models.transaction.Rule."""
    client = _get_client()
    if not client:
        return []
    try:
        rows = client.table("firm_rules").select("*").eq("firm_id", firm_id).execute().data or []
    except Exception as exc:
        print(f"  [DB] Failed to load rules: {exc}")
        return []

    rules = []
    for row in rows:
        platform = row.get("platform")
        rules.append({
            # '*' is how a firm-wide rule is stored; Rule expects None for it.
            "platform":   None if platform in (None, "*") else platform,
            "label":      row.get("label"),
            "account":    row.get("account"),
            "side":       row.get("side"),
            "decided_by": row.get("decided_by"),
            "decided_at": row.get("decided_at"),
        })
    return rules


# ── Settlement files ──────────────────────────────────────────────────────────

def save_settlement_file(
    firm_id: str, filename: str, cycle: str, raw: bytes, line_count: int,
    reported: str = "",
) -> bool:
    """Retain a source settlement file.

    Traceability was the requirement every accountant raised independently: a
    posted figure has to be walkable back to the file it came from.
    """
    ok = _upsert("settlement_files", {
        "id":         f"{firm_id}|{cycle}|{filename}",
        "firm_id":    firm_id,
        "cycle":      cycle,
        "filename":   filename,
        "line_count": line_count,
        "csv_data":   raw.decode("utf-8-sig", errors="replace"),
        # The payout figures typed at upload, where the file did not state
        # them. Without these a rebuilt cycle would reconcile against zero
        # and report the whole payout as a residual.
        "reported":   reported or "",
    }, optional=("reported",))
    if ok:
        print(f"  [DB] Settlement file retained: {filename} ({cycle})")
    return ok


def load_settlement_files(firm_id: str, cycle: Optional[str] = None) -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        query = client.table("settlement_files").select("*").eq("firm_id", firm_id)
        if cycle:
            query = query.eq("cycle", cycle)
        return query.execute().data or []
    except Exception as exc:
        print(f"  [DB] Failed to load settlement files: {exc}")
        return []


# ── Cycle resolutions ─────────────────────────────────────────────────────────

def save_resolution(firm_id: str, cycle: str, key: str, account: str,
                    side: str, actor: str = "") -> bool:
    """Remember a decision that is about one settlement, not about a label."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("cycle_resolutions").upsert({
            "firm_id": firm_id, "cycle": cycle, "key": key,
            "account": account, "side": side, "actor": actor,
        }, on_conflict="firm_id,cycle,key").execute()
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save resolution: {exc}")
        return False


def load_resolutions(firm_id: str, cycle: str) -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return (client.table("cycle_resolutions").select("*")
                .eq("firm_id", firm_id).eq("cycle", cycle).execute().data or [])
    except Exception as exc:
        print(f"  [DB] Failed to load resolutions: {exc}")
        return []


def update_settlement_reported(firm_id: str, cycle: str, reported: str) -> bool:
    """Record a payout figure supplied after the file was uploaded.

    Written to every file in the cycle rather than one, so a rebuild reaches the
    same answer whichever order it reads them in.
    """
    client = _get_client()
    if not client:
        return False
    try:
        (client.table("settlement_files").update({"reported": reported})
         .eq("firm_id", firm_id).eq("cycle", cycle).execute())
        return True
    except Exception as exc:
        print(f"  [DB] Failed to record reported payout: {exc}")
        return False


# ── Posted entries ────────────────────────────────────────────────────────────

def save_posted_entry(
    firm_id: str, cycle: str, entry, adapter: str, actor: str, audit_csv: str
) -> bool:
    """Record a journal that was sent to a ledger, with the working paper."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("posted_entries").insert({
            "firm_id":   firm_id,
            "cycle":     cycle,
            "platform":  entry.platform.value,
            "reference": entry.reference,
            "adapter":   adapter,
            "actor":     actor,
            "lines":     json.dumps([l.model_dump(mode="json") for l in entry.lines]),
            "audit_csv": audit_csv,
        }).execute()
        print(f"  [DB] Posted entry recorded: {entry.reference}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to record posted entry: {exc}")
        return False


def load_posted_entries(firm_id: str, cycle: Optional[str] = None) -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        query = client.table("posted_entries").select("*").eq("firm_id", firm_id)
        if cycle:
            query = query.eq("cycle", cycle)
        rows = query.order("posted_at", desc=True).execute().data or []
        for row in rows:
            if isinstance(row.get("lines"), str):
                row["lines"] = json.loads(row["lines"])
        return rows
    except Exception as exc:
        print(f"  [DB] Failed to load posted entries: {exc}")
        return []


# ── Settlements ───────────────────────────────────────────────────────────────

def save_settlement(firm_id: str, row: dict) -> bool:
    """Upsert one settlement: a payout, and the document it becomes.

    Callers write either half of the row — what Fynn computed, or what was sent
    — never both at once, so a reconciliation run cannot overwrite the record of
    a post, and a post cannot overwrite a newer reconciliation.
    """
    return _upsert("settlements", {"firm_id": firm_id, **row},
                   on_conflict="firm_id,reference")


def load_settlements(firm_id: str, cycle: Optional[str] = None) -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        query = client.table("settlements").select("*").eq("firm_id", firm_id)
        if cycle:
            query = query.eq("cycle", cycle)
        rows = query.execute().data or []
        for row in rows:
            for field in ("lines", "posted_lines"):
                if isinstance(row.get(field), str):
                    row[field] = json.loads(row[field])
        return rows
    except Exception as exc:
        print(f"  [DB] Failed to load settlements: {exc}")
        return []


def delete_settlement(firm_id: str, reference: str) -> bool:
    """Forget a settlement that no longer exists and was never sent.

    A month with one payout posts as one document; a second file can split it
    into several. The single one then describes nothing, and left in the list it
    would offer to post a document that double-counts the others.
    """
    client = _get_client()
    if not client:
        return False
    try:
        (client.table("settlements").delete()
         .eq("firm_id", firm_id).eq("reference", reference).execute())
        return True
    except Exception as exc:
        print(f"  [DB] Failed to remove settlement {reference}: {exc}")
        return False


def load_settlement_months(firm_id: str) -> list[str]:
    """Every month with a retained file, without reading the files."""
    client = _get_client()
    if not client:
        return []
    try:
        rows = (client.table("settlement_files").select("cycle")
                .eq("firm_id", firm_id).execute().data or [])
        return sorted({r["cycle"] for r in rows if r.get("cycle")})
    except Exception as exc:
        print(f"  [DB] Failed to list months: {exc}")
        return []


# ── Account mappings ──────────────────────────────────────────────────────────

def save_account_mapping(firm_id: str, ledger: str, account: str, entry) -> bool:
    """Upsert one Fynn account → ledger account mapping."""
    client = _get_client()
    if not client:
        return False
    row = {
        "firm_id":      firm_id,
        "ledger":       ledger,
        "fynn_account": account,
        "code":         entry.code,
        "name":         entry.name,
        "tax":          getattr(entry, "tax", "") or "",
    }
    try:
        client.table("account_mappings").upsert(
            row, on_conflict="firm_id,ledger,fynn_account").execute()
        print(f"  [DB] Account mapped for {ledger}: {account} → {entry.code}")
        return True
    except Exception as exc:
        # `tax` arrived after this table did. Keep the mapping rather than lose
        # it over a column the database has not been told about yet.
        if "tax" in str(exc):
            try:
                row.pop("tax", None)
                client.table("account_mappings").upsert(
                    row, on_conflict="firm_id,ledger,fynn_account").execute()
                print(f"  [DB] Account mapped for {ledger}: {account} → "
                      f"{entry.code} (without tax — run migration 012)")
                return True
            except Exception as retry_exc:
                exc = retry_exc
        print(f"  [DB] Failed to save account mapping: {exc}")
        return False


def delete_account_mapping(firm_id: str, ledger: str, account: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        (client.table("account_mappings").delete()
         .eq("firm_id", firm_id).eq("ledger", ledger)
         .eq("fynn_account", account).execute())
        return True
    except Exception as exc:
        print(f"  [DB] Failed to clear account mapping: {exc}")
        return False


def load_account_mappings(firm_id: str, ledger: str) -> list[dict]:
    client = _get_client()
    if not client:
        return []
    try:
        return (client.table("account_mappings").select("*")
                .eq("firm_id", firm_id).eq("ledger", ledger).execute().data or [])
    except Exception as exc:
        print(f"  [DB] Failed to load account mappings: {exc}")
        return []


# ── Diagnostics ───────────────────────────────────────────────────────────────

EXPECTED_TABLES = (
    "users", "firm_profiles", "firm_rules", "settlement_files",
    "posted_entries", "account_mappings", "ledger_connections",
    "cycle_resolutions", "settlements",
)


# ── Users ─────────────────────────────────────────────────────────────────────

def load_users() -> list[dict]:
    """Every account. Small by nature — one row per person, not per document."""
    client = _get_client()
    if not client:
        return []
    try:
        return client.table("users").select("*").execute().data or []
    except Exception as exc:
        print(f"  [DB] Could not load users: {exc}")
        return []


def save_user(row: dict) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        client.table("users").upsert(row, on_conflict="id").execute()
        return True
    except Exception as exc:
        print(f"  [DB] Failed to store user: {exc}")
        return False


def diagnose() -> dict:
    """Why persistence is or is not working, in the order things fail.

    Every write in this module is best-effort and silent on failure, which is
    right for keeping a close running through an outage but means a
    misconfiguration looks exactly like working software. This says which it is.

    Never returns the service key, or anything derived from it.
    """
    import re
    import socket

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    out: dict = {
        "configured": bool(url and key),
        "url_set": bool(url),
        "key_set": bool(key),
        "host": re.sub(r"^https?://", "", url).rstrip("/").split("/")[0] if url else "",
        "dns": None,
        "connected": False,
        "tables": {},
        "detail": "",
    }
    if not out["configured"]:
        missing = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_SERVICE_KEY", key)) if not v]
        out["detail"] = f"Not configured: {', '.join(missing)} unset. Nothing is persisted."
        return out

    try:
        socket.getaddrinfo(out["host"], 443, proto=socket.IPPROTO_TCP)
        out["dns"] = True
    except Exception as exc:
        out["dns"] = False
        out["detail"] = (
            f"{out['host']} does not resolve ({type(exc).__name__}). The project is "
            "probably paused or deleted, or the URL has a typo. Nothing is persisted."
        )
        return out

    client = _get_client()
    if client is None:
        out["detail"] = "Credentials are set but the client would not start."
        return out

    for table in EXPECTED_TABLES:
        # Six probes means six chances to be unlucky. A pooled connection that
        # Supabase has already closed fails once and succeeds on a fresh one,
        # and reporting that as "not usable" is indistinguishable from a real
        # schema problem — which is the one thing this screen exists to tell
        # apart. A missing table is deterministic, so retrying costs nothing.
        for attempt in (1, 2):
            try:
                result = client.table(table).select("*", count="exact").limit(1).execute()
                out["tables"][table] = {"ok": True, "rows": result.count}
                out["connected"] = True
                break
            except Exception as exc:
                message = str(exc)
                missing = "does not exist" in message or "PGRST205" in message
                if not missing and attempt == 1:
                    continue
                out["tables"][table] = {
                    "ok": False,
                    "error": "table missing — run the migrations" if missing else message[:160],
                }

    if out["connected"] and all(t["ok"] for t in out["tables"].values()):
        out["detail"] = "Connected. Every table is present."
    elif out["connected"]:
        broken = [n for n, t in out["tables"].items() if not t["ok"]]
        out["detail"] = f"Connected, but these tables are not usable: {', '.join(broken)}"
    else:
        out["detail"] = "Reached the host but no table could be read."
    return out


# ── Ledger connections (OAuth tokens) ─────────────────────────────────────────

def save_connection(firm_id: str, ledger: str, tokens: dict) -> bool:
    """Store or replace a firm's tokens for one ledger."""
    client = _get_client()
    if not client:
        return False
    row = {
        "firm_id":       firm_id,
        "ledger":        ledger,
        "access_token":  tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at":    tokens["expires_at"],
        "org_id":        tokens.get("org_id", ""),
        "org_name":      tokens.get("org_name", ""),
        "scopes":        tokens.get("scopes", ""),
    }
    try:
        client.table("ledger_connections").upsert(
            row, on_conflict="firm_id,ledger").execute()
        print(f"  [DB] {ledger} connection stored for {firm_id}")
        return True
    except Exception as exc:
        # `scopes` arrived after this table did. A database created from the
        # original 009 has no such column, and losing the whole connection over
        # a field that only affects a warning would be a poor trade — so drop
        # it and keep the tokens, which are the part that cannot be recovered.
        if "scopes" in str(exc):
            try:
                row.pop("scopes", None)
                client.table("ledger_connections").upsert(
                    row, on_conflict="firm_id,ledger").execute()
                print(f"  [DB] {ledger} connection stored for {firm_id} "
                      f"(without scopes — run migration 010)")
                return True
            except Exception as retry_exc:
                exc = retry_exc
        print(f"  [DB] Failed to store {ledger} connection: {exc}")
        return False


def load_connection(firm_id: str, ledger: str) -> Optional[dict]:
    client = _get_client()
    if not client:
        return None
    try:
        result = (client.table("ledger_connections").select("*")
                  .eq("firm_id", firm_id).eq("ledger", ledger).execute())
        return result.data[0] if result.data else None
    except Exception as exc:
        print(f"  [DB] Failed to load {ledger} connection: {exc}")
        return None


def delete_connection(firm_id: str, ledger: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        (client.table("ledger_connections").delete()
         .eq("firm_id", firm_id).eq("ledger", ledger).execute())
        return True
    except Exception as exc:
        print(f"  [DB] Failed to delete {ledger} connection: {exc}")
        return False
