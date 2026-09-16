"""Settlements: the list an accountant works from, one row per payout.

A2X lists settlements, not months, and for a mechanical reason: a settlement is
one bank deposit, and one document in the ledger that the deposit is matched
to. Fynn already split a month into exactly those documents to post them; this
module makes each one something you can find again, open, post and follow into
Xero.

The month stays where decisions are made. Exceptions and the residual are about
a platform's whole month, so every settlement in that month shares its state
until they are resolved, and a settlement cannot be posted ahead of its month.

Nothing here touches storage or the web layer. It turns a reconciled cycle into
rows, and rows into a status, so the two can be tested without either.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Optional

from models.transaction import JournalEntry, Platform
from services.ledger import _cycle_end, _payout_date, split_clearing
from services.reconciliation import is_blocked, resolution_for

# What Xero calls a document once it has left Fynn's hands. A draft is still
# Fynn's to correct; anything past it belongs to the accountant and the ledger.
EDITABLE_IN_LEDGER = {"", "DRAFT", "SUBMITTED"}
GONE_FROM_LEDGER = {"VOIDED", "DELETED"}
FINAL_IN_LEDGER = {"PAID", "VOIDED", "DELETED"}

STATUS_LABELS = {
    "needs_review": "Needs review",
    "ready":        "Ready to post",
    "prepared":     "Dry run only",
    "draft":        "Draft in Xero",
    "changed":      "Changed since sent",
    "approved":     "Approved in Xero",
    "reconciled":   "Reconciled",
}


def documents(cycle, per_payout: bool) -> list[tuple[JournalEntry, object]]:
    """Every document this cycle would post, with the result it came from.

    The same choice the post makes: one document per payout where the ledger
    reconciles against a document, the platform's month entry otherwise. The
    list and the post must agree, or the list offers documents that never get
    sent.
    """
    out = []
    for _platform, result in cycle.run().items():
        entries = (result.payout_journals if per_payout and result.payout_journals
                   else [result.journal])
        out.extend((entry, result) for entry in entries if entry is not None)
    return out


def _lines(entry: JournalEntry) -> list[dict]:
    return [{"account": l.account, "side": l.side.value, "amount": round(l.amount, 2)}
            for l in entry.lines]


def total_of(entry: JournalEntry) -> float:
    """What the deposit will be: the clearing line, signed."""
    clearing, _ = split_clearing(entry)
    if clearing is None:
        return 0.0
    return round(clearing.amount if clearing.side.value == "debit" else -clearing.amount, 2)


def total_of_lines(lines: list[dict]) -> float:
    """The same, from lines as they are stored."""
    for l in lines or []:
        if str(l.get("account", "")).lower().endswith("clearing account"):
            amount = float(l.get("amount") or 0)
            return round(amount if l.get("side") == "debit" else -amount, 2)
    return 0.0


def computed_row(entry: JournalEntry, result, cycle=None) -> dict:
    """The half of a settlement row Fynn recomputes on every reconciliation."""
    period = entry.payout
    if not period and cycle is not None:
        # A month entry covering one stated period — a single weekly file — is
        # still that period, and the list should say which.
        stated = {l.payout for l in cycle.lines
                  if l.platform == entry.platform and l.payout and l.payout != cycle.cycle}
        period = stated.pop() if len(stated) == 1 else ""
    return {
        "reference": entry.reference,
        "cycle": entry.cycle,
        "platform": entry.platform.value,
        "period": period or entry.cycle,
        "period_end": _payout_date(period) or _cycle_end(entry.cycle),
        "total": total_of(entry),
        "lines": _lines(entry),
        "open_exceptions": len([e for e in result.exceptions if not e.resolved]),
    }


def _one_date(text: str):
    """A single date from a platform's wording, or None."""
    from datetime import datetime
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def period_label(period: str, cycle: str = "") -> str:
    """The settlement period as the list shows it, one shape for every platform.

    Platforms word their periods differently — Lazada states a range, Shopee's
    monthly statement states none at all and falls back to the cycle — and the
    list read as two different systems. Dates are rendered one way here.

    A month stays a month: a statement that covers January is not evidence that
    the payout ran 01 to 31 January, and writing those dates would put a
    precision in the accountant's list that the file never stated.
    """
    import re as _re
    text = (period or cycle or "").strip()
    if not text:
        return ""

    month = _re.fullmatch(r"(\d{4})-(\d{2})", text)
    if month:
        from datetime import date
        year, number = int(month.group(1)), int(month.group(2))
        if 1 <= number <= 12:
            return date(year, number, 1).strftime("%b %Y")
        return text

    parts = _re.split(r"\s+(?:-|–|—|to)\s+", text)
    dates = [_one_date(p) for p in parts]
    if len(dates) == 2 and all(dates):
        return f"{dates[0]:%d %b %Y} – {dates[1]:%d %b %Y}"
    if len(dates) == 1 and dates[0]:
        return f"{dates[0]:%d %b %Y}"
    return text          # wording Fynn cannot read stays the platform's own


def _same_lines(a, b) -> bool:
    def norm(lines):
        return sorted((l.get("account"), l.get("side"), round(float(l.get("amount") or 0), 2))
                      for l in (lines or []))
    return norm(a) == norm(b)


def status_of(row: dict) -> str:
    """One word for where a settlement is, from Fynn's side and the ledger's."""
    if not row.get("posted_at"):
        return "needs_review" if row.get("open_exceptions") else "ready"

    ledger_status = (row.get("ledger_status") or "").upper()
    if row.get("ledger") == "dry-run":
        base = "prepared"
    elif ledger_status == "PAID":
        return "reconciled"
    elif ledger_status == "AUTHORISED":
        return "approved"
    elif ledger_status in GONE_FROM_LEDGER:
        # Nothing of this settlement is left in Xero, so there is no ledger
        # state left to report: it stands where an unsent settlement stands.
        # Saying "Voided in Xero" here also hid the two things that matter more
        # — that the month has open exceptions, or that the figures changed
        # since the voided document was sent.
        return "needs_review" if row.get("open_exceptions") else "ready"
    else:
        base = "draft"

    # Still Fynn's to correct, so say when it no longer matches what was sent.
    if row.get("open_exceptions"):
        return "needs_review"
    if not _same_lines(row.get("lines"), row.get("posted_lines")):
        return "changed"
    return base


def was_voided(row: dict) -> bool:
    """Whether Fynn sent this settlement and the document is gone from Xero.

    No longer the settlement's status — it is a fact about a document that once
    existed, kept because it explains why a sent settlement is asking to be
    sent again, and because an automatic post must not quietly undo somebody's
    decision to remove it.
    """
    return (bool(row.get("posted_at"))
            and row.get("ledger") not in ("", "dry-run")
            and (row.get("ledger_status") or "").upper() in GONE_FROM_LEDGER)


def can_send(row: dict) -> tuple[bool, str]:
    """Whether Post or Re-send is on offer, and if not, why not."""
    if row.get("open_exceptions"):
        n = row["open_exceptions"]
        return False, (f"{row.get('platform')} {row.get('cycle')} has {n} open "
                       f"{'exception' if n == 1 else 'exceptions'}. Resolve "
                       f"{'it' if n == 1 else 'them'} first — "
                       "each one changes this payout.")
    if not row.get("lines"):
        return False, "Fynn has not reconciled this settlement since the last restart."
    return ledger_allows(row)


def ledger_allows(row: dict) -> tuple[bool, str]:
    """Whether the ledger's copy is still Fynn's to replace."""
    ledger_status = (row.get("ledger_status") or "").upper()
    if row.get("posted_at") and row.get("ledger") not in ("", "dry-run") \
            and ledger_status not in EDITABLE_IN_LEDGER | GONE_FROM_LEDGER:
        state = "reconciled" if ledger_status == "PAID" else "approved"
        return False, (f"Already {state} in Xero, so Fynn will not change it. Edit "
                       "it in Xero, or void it there and send it again.")
    return True, ""


def present(row: dict) -> dict:
    """A row as the list and the detail page show it."""
    status = status_of(row)
    sendable, why_not = can_send(row)
    posted = bool(row.get("posted_at"))
    return {
        "reference": row["reference"],
        "cycle": row.get("cycle", ""),
        "platform": row.get("platform", ""),
        "period": row.get("period", ""),
        "period_end": row.get("period_end", ""),
        "total": round(float(row.get("total") or 0), 2),
        "posted_total": (round(float(row["posted_total"]), 2)
                         if row.get("posted_total") is not None else None),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "period_label": period_label(row.get("period", ""), row.get("cycle", "")),
        "was_voided": was_voided(row),
        "open_exceptions": row.get("open_exceptions") or 0,
        "can_send": sendable,
        "why_not": why_not,
        "action": ("review" if status == "needs_review" else
                   # A sent settlement asking to go again: its figures moved,
                   # it was only a dry run, or its document is gone from Xero.
                   "resend" if posted and sendable and status in ("changed", "prepared", "ready")
                   else "post" if not posted and sendable else ""),
        "ledger": row.get("ledger", ""),
        "ledger_id": row.get("ledger_id", ""),
        "ledger_status": row.get("ledger_status", ""),
        "document": row.get("document", ""),
        "ledger_url": ledger_url(row),
        "posted_at": row.get("posted_at") or "",
        "posted_by": row.get("posted_by", ""),
        "status_checked_at": row.get("status_checked_at") or "",
    }


def ledger_url(row: dict) -> str:
    """A link that opens the document in Xero, when there is one to open.

    Opens in whichever organisation the accountant last had selected in Xero,
    which for a firm with one connection is the right one.
    """
    if row.get("ledger") != "xero":
        return ""
    if row.get("ledger_id"):
        area = "AccountsPayable" if row.get("document") == "ACCPAY" else "AccountsReceivable"
        return f"https://go.xero.com/{area}/View.aspx?InvoiceID={row['ledger_id']}"
    return ""


def merge(stored: list[dict], live: list[dict]) -> list[dict]:
    """Stored rows, with what was just computed laid over the computed half.

    Live wins for the figures, because it is the reconciliation as it stands in
    this process; storage wins for what was sent, because only storage saw the
    post if it happened before a restart.
    """
    by_ref = {r["reference"]: dict(r) for r in stored}
    for row in live:
        by_ref.setdefault(row["reference"], {}).update(row)
    return list(by_ref.values())


def sort_key(row: dict):
    """Newest settlement first, as A2X lists them."""
    return (row.get("period_end") or row.get("cycle") or "", row.get("reference", ""))


# ── inside one settlement ─────────────────────────────────────────────────────

def lines_of(cycle, entry: JournalEntry) -> list:
    """The settlement lines behind one document."""
    return [l for l in cycle.lines
            if l.platform == entry.platform
            and (not entry.payout or (l.payout or cycle.cycle) == entry.payout)]


def breakdown(cycle, entry: JournalEntry, result, accounts=None) -> list[dict]:
    """What the payout is made of, fee by fee, and where each one posts.

    The document Xero receives is summarised by account, which is right for the
    ledger and useless for checking: "Commission Expense −412.09" says nothing
    about which fees made it. A2X shows the settlement's own labels beside the
    account each lands on, and that is what an accountant reviews.
    """
    blocked = {e.key for e in result.exceptions if not e.resolved}
    grouped: dict[tuple, dict] = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for line in lines_of(cycle, entry):
        if is_blocked(line, blocked):
            account, state = "", "open"
        else:
            decided = resolution_for(line, cycle.resolutions)
            rule = None if decided else cycle.store.find(line)
            account = decided[0] if decided else (rule.account if rule else "")
            state = "decided" if decided else ("rule" if rule else "open")
        item = grouped[(line.label, account, state)]
        item["amount"] += line.amount
        item["count"] += 1

    out = []
    for (label, account, state), item in grouped.items():
        mapped = accounts.get(account) if (accounts is not None and account) else None
        out.append({
            "label": label,
            "count": item["count"],
            "amount": round(item["amount"], 2),
            "account": account,
            "code": getattr(mapped, "code", "") or "",
            "tax": getattr(mapped, "tax", "") or "",
            "state": state,
        })

    # The residual lands on the month's last document only; show it where it lands.
    residual_key = f"{entry.platform.value}|{cycle.cycle}|residual"
    residual = cycle.resolutions.get(residual_key)
    last = not entry.payout or not result.payout_journals \
        or result.payout_journals[-1].reference == entry.reference
    if residual and last and abs(result.residual) >= 0.005:
        mapped = accounts.get(residual[0]) if accounts is not None else None
        out.append({"label": "Residual — difference to the reported payout", "count": 0,
                    "amount": round(-result.residual, 2), "account": residual[0],
                    "code": getattr(mapped, "code", "") or "",
                    "tax": getattr(mapped, "tax", "") or "", "state": "decided"})

    return sorted(out, key=lambda r: (r["state"] != "open", -abs(r["amount"])))


def raw_csv(lines: list) -> str:
    """The settlement's source rows, as a file — A2X's "download the raw data"."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Platform", "Settlement period", "Date", "Order", "Label", "Category",
                "Amount", "Source file"])
    for l in lines:
        w.writerow([l.platform.value, l.payout or l.cycle, l.date or "", l.order_id or "",
                    l.label, l.category or "", f"{l.amount:.2f}", l.source_ref or ""])
    return buf.getvalue()


def platform_of(value: str) -> Optional[Platform]:
    try:
        return Platform(value)
    except ValueError:
        return None
