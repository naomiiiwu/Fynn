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


# ── Firm profiles ─────────────────────────────────────────────────────────────

def save_firm(profile) -> bool:
    """Upsert one firm profile."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("firm_profiles").upsert({
            "workspace_id":    profile.id,
            "firm":            profile.firm,
            "actor":           profile.actor,
            "platforms":       profile.platforms,
            "ledger":          profile.ledger,
            "onboarding_step": profile.onboarding_step,
        }).execute()
        print(f"  [DB] Firm profile saved for {profile.id}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save firm profile: {exc}")
        return False


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
    firm_id: str, filename: str, cycle: str, raw: bytes, line_count: int
) -> bool:
    """Retain a source settlement file.

    Traceability was the requirement every accountant raised independently: a
    posted figure has to be walkable back to the file it came from.
    """
    client = _get_client()
    if not client:
        return False
    try:
        client.table("settlement_files").upsert({
            "id":         f"{firm_id}|{cycle}|{filename}",
            "firm_id":    firm_id,
            "cycle":      cycle,
            "filename":   filename,
            "line_count": line_count,
            "csv_data":   raw.decode("utf-8-sig", errors="replace"),
        }).execute()
        print(f"  [DB] Settlement file retained: {filename} ({cycle})")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to retain settlement file: {exc}")
        return False


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


# ── Account mappings ──────────────────────────────────────────────────────────

def save_account_mapping(firm_id: str, ledger: str, account: str, entry) -> bool:
    """Upsert one Fynn account → ledger account mapping."""
    client = _get_client()
    if not client:
        return False
    try:
        client.table("account_mappings").upsert(
            {
                "firm_id":      firm_id,
                "ledger":       ledger,
                "fynn_account": account,
                "code":         entry.code,
                "name":         entry.name,
            },
            on_conflict="firm_id,ledger,fynn_account",
        ).execute()
        print(f"  [DB] Account mapped for {ledger}: {account} → {entry.code}")
        return True
    except Exception as exc:
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
    "firm_profiles", "firm_rules", "settlement_files",
    "posted_entries", "account_mappings",
)


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
        try:
            result = client.table(table).select("*", count="exact").limit(1).execute()
            out["tables"][table] = {"ok": True, "rows": result.count}
            out["connected"] = True
        except Exception as exc:
            message = str(exc)
            missing = "does not exist" in message or "PGRST205" in message
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
