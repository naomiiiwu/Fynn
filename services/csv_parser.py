"""Settlement CSV parsing.

Fynn's canonical shape is `platform,cycle,order,label,amount,date`, but no
marketplace exports that. Real files arrive with the platform's own column
names, the platform implied only by the filename, and the payout stated on a
row inside the file rather than passed in separately. This module maps what
actually arrives onto SettlementLine, and says clearly what it could not map —
an unreadable file has to fail loudly, never quietly produce a short cycle.

The label itself is passed through untouched. Deciding what a label *means* is
the rules engine's job (services/classification.py), and inventing a mapping
here would put an ungoverned second classifier in the pipeline.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional

from models.transaction import Platform, SettlementLine


class SettlementParseError(ValueError):
    """Raised when a file cannot be read as a settlement export."""


# Column aliases, lowercased. Matched exact-first, then as a substring, so
# "Amount (MYR)" resolves through the bare "amount" entry.
_COLUMNS: dict[str, list[str]] = {
    "platform": ["platform", "marketplace", "channel", "shop"],
    "cycle":    ["cycle", "period", "settlement period", "statement period", "payout period"],
    "label":    ["label", "type", "transaction type", "fee type", "description", "remarks",
                 "item", "particulars"],
    "amount":   ["amount", "net amount", "total amount", "value", "settlement amount"],
    "order":    ["order", "order id", "order no.", "order no", "order sn", "order number",
                 "reference", "ref no."],
    "date":     ["date", "transaction date", "created at", "created time", "settlement date",
                 "payout date"],
    "credit":   ["credit"],
    "debit":    ["debit"],
}

# Rows that state what the platform paid out. These are not settlement lines —
# they are the figure Fynn reconciles the settlement lines against, so folding
# them into the total would make every cycle tie out trivially and wrongly.
PAYOUT_LABELS = {
    "payout", "total payout", "settlement", "total settlement amount", "withdrawal",
    "released amount", "amount released", "bank transfer", "net payout", "paid out",
}

_PLATFORM_HINTS: list[tuple[str, Platform]] = [
    ("shopee", Platform.SHOPEE),
    ("lazada", Platform.LAZADA),
    ("tiktok", Platform.TIKTOK),
    ("tik tok", Platform.TIKTOK),
]

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
)


@dataclass
class ParsedSettlement:
    """Everything one uploaded file yielded."""

    lines: list[SettlementLine] = field(default_factory=list)
    reported_payouts: dict[Platform, float] = field(default_factory=dict)
    cycles: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)

    @property
    def cycle(self) -> str:
        """The dominant cycle in the file, for labelling the close."""
        if not self.cycles:
            return "unknown"
        return sorted(self.cycles)[-1]

    @property
    def platforms(self) -> set[Platform]:
        return {l.platform for l in self.lines}


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our field names onto the file's actual headers."""
    headers = {(h or "").strip().lower(): h for h in fieldnames}
    resolved: dict[str, str] = {}
    for field_name, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in headers:
                resolved[field_name] = headers[alias]
                break
        else:
            # Fall back to a substring match: "amount (myr)" contains "amount".
            for alias in aliases:
                match = next((h for h in headers if alias in h), None)
                if match:
                    resolved[field_name] = headers[match]
                    break
    return resolved


def _parse_amount(raw: str) -> Optional[float]:
    """Strip currency symbols, thousands separators and bracketed negatives."""
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _cycle_from_date(raw: str) -> Optional[str]:
    """Derive a YYYY-MM cycle from a date cell, for files with no period column."""
    from datetime import datetime

    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    # Last resort: a leading YYYY-MM is unambiguous even in an unknown format.
    match = re.match(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    return None


def platform_from_text(text: str) -> Optional[Platform]:
    """Identify a platform from a cell value or a filename."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    for hint, platform in _PLATFORM_HINTS:
        if hint in lowered:
            return platform
    return None


def parse_reported(reported: str) -> dict[Platform, float]:
    """Parse `"Lazada=1038;Shopee=725"` into reported payouts.

    Raises SettlementParseError on anything it cannot read — a mistyped payout
    silently dropped would show up later as an unexplained residual.
    """
    payouts: dict[Platform, float] = {}
    for part in filter(None, (p.strip() for p in (reported or "").split(";"))):
        name, sep, amount = part.partition("=")
        platform = platform_from_text(name)
        value = _parse_amount(amount) if sep else None
        if platform is None or value is None:
            raise SettlementParseError(
                f'Could not read reported payout "{part}". '
                'Use the form Lazada=1038.00;Shopee=725.00'
            )
        payouts[platform] = value
    return payouts


def parse_settlement_csv(
    raw: bytes,
    filename: str = "upload.csv",
    default_platform: Optional[Platform] = None,
    default_cycle: Optional[str] = None,
) -> ParsedSettlement:
    """Read one settlement export into SettlementLines.

    The platform is taken from a column when the file has one, otherwise from
    the filename, otherwise from `default_platform`. A file that resolves to no
    platform at all is rejected rather than guessed at.
    """
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SettlementParseError("That file has no header row.")

    cols = _resolve_columns(list(reader.fieldnames))
    missing = [f for f in ("label",) if f not in cols]
    if "amount" not in cols and not ("credit" in cols or "debit" in cols):
        missing.append("amount")
    if missing:
        raise SettlementParseError(
            "Missing required column(s): " + ", ".join(missing)
            + ". Detected headers: " + ", ".join(reader.fieldnames[:12])
        )

    file_platform = platform_from_text(filename) or default_platform
    parsed = ParsedSettlement()

    for row_number, row in enumerate(reader, start=2):
        label = (row.get(cols["label"]) or "").strip()
        if not label:
            parsed.skipped.append(f"row {row_number}: no label")
            continue

        platform = (
            platform_from_text(row.get(cols["platform"], "")) if "platform" in cols else None
        ) or file_platform
        if platform is None:
            parsed.skipped.append(
                f"row {row_number}: no platform column, and the filename does not name one"
            )
            continue

        if "amount" in cols:
            amount = _parse_amount(row.get(cols["amount"], ""))
        else:
            credit = _parse_amount(row.get(cols.get("credit", ""), "")) or 0.0
            debit = _parse_amount(row.get(cols.get("debit", ""), "")) or 0.0
            amount = credit - abs(debit)
        if amount is None:
            parsed.skipped.append(f"row {row_number}: unreadable amount")
            continue

        date = (row.get(cols["date"], "") or "").strip() if "date" in cols else ""
        cycle = (row.get(cols["cycle"], "") or "").strip() if "cycle" in cols else ""
        cycle = cycle or _cycle_from_date(date) or default_cycle or "unknown"

        if label.strip().lower() in PAYOUT_LABELS:
            # Reported payouts are stated positive; a withdrawal row is often
            # negative because it leaves the platform balance.
            parsed.reported_payouts[platform] = abs(amount)
            parsed.cycles.add(cycle)
            continue

        order_id = (row.get(cols["order"], "") or "").strip() if "order" in cols else ""
        parsed.lines.append(
            SettlementLine(
                platform=platform,
                cycle=cycle,
                label=label,
                amount=amount,
                order_id=order_id or None,
                date=date or None,
                source_ref=filename,
            )
        )
        parsed.cycles.add(cycle)

    if not parsed.lines:
        detail = f" ({parsed.skipped[0]})" if parsed.skipped else ""
        raise SettlementParseError(f"No settlement lines could be read from that file{detail}.")

    return parsed
