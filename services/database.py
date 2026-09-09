"""Supabase persistence for Fynn.

Tables (migrations/005_reconciliation.sql):
  firm_profiles      — one row per accounting firm, keyed by WhatsApp number
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
            "phone":           profile.phone,
            "firm":            profile.firm,
            "actor":           profile.actor,
            "language":        profile.language,
            "platforms":       profile.platforms,
            "ledger":          profile.ledger,
            "onboarding_step": profile.onboarding_step,
        }).execute()
        print(f"  [DB] Firm profile saved for {profile.phone}")
        return True
    except Exception as exc:
        print(f"  [DB] Failed to save firm profile: {exc}")
        return False


def load_firm(phone: str) -> Optional[dict]:
    client = _get_client()
    if not client:
        return None
    try:
        result = client.table("firm_profiles").select("*").eq("phone", phone).execute()
        return result.data[0] if result.data else None
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
