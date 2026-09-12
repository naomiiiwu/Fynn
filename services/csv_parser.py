"""Settlement file parsing.

Fynn's canonical shape is `platform,cycle,order,label,amount,date`, but no
marketplace exports that. Two structures arrive in practice:

  long   one row per fee line, carrying its own name and amount.
         Lazada's account statement, and Shopee's order adjustments.
  wide   one row per order, fees as columns, with a total column that the
         components sum to. Shopee's income statement.

Both are melted into SettlementLine. The label is passed through untouched —
deciding what a label *means* is the rules engine's job
(services/classification.py), and inventing a mapping here would put an
ungoverned second classifier in the pipeline.

What this module deliberately does not assume, because none of it is verified
against a real export:

  column order      every column is found by name, never by position
  header casing     matching is case- and whitespace-insensitive
  file format       .csv and .xlsx both parse
  file layout       adjustments may be their own file or a second block of
                    rows inside one, under their own header
  per-country cols  in a wide file, any column that is not recognised meta is
                    treated as a fee, so an extra tax or levy column in another
                    market becomes a line rather than being silently dropped

The one thing a wide file *is* checked against is its own arithmetic: the
components of every row must sum to its stated total. That identity is the
only part of the layout worth trusting, so a file that fails it is reported
rather than reconciled — a mis-read column would otherwise surface later as an
unexplained residual.
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
    # No bare "shop": it matches Shopee's own fee columns ("Shopee Discount")
    # and would read a money column as a platform name.
    "platform": ["platform", "marketplace", "channel", "shop name", "store"],
    "cycle":    ["statement", "statement period", "cycle", "period", "payout period"],
    "label":    ["label", "fee name", "adjustment reason", "type", "transaction type",
                 "fee type", "description", "remarks", "reason", "item", "particulars"],
    "amount":   ["amount", "released amount", "amount released", "net amount",
                 "total amount", "value", "settlement amount"],
    "order":    ["order", "order id", "order no.", "order no", "order sn", "order number",
                 "reference", "ref no."],
    "date":     ["date", "transaction date", "adjustment date", "created at", "created time",
                 "release time", "settlement date", "payout date"],
    "credit":   ["credit"],
    "debit":    ["debit"],
    # Lazada's "Fee Classification". Kept because it is the platform's own
    # taxonomy: it groups fee names nobody has seen yet under a heading a firm
    # has already made a decision about.
    "category": ["fee classification", "classification", "fee category", "category",
                 "fee type"],
    # Free text the platform attached to the line, and the reference it filed it
    # under. Both appear in Lazada's Transaction Overview export.
    "note":     ["comment", "note", "memo", "item name", "description detail"],
}

# In a wide file these identify the row rather than a fee it carries. Kept
# deliberately narrow: anything not matched here becomes a fee line, so an
# over-broad needle silently deletes money. "buyer" was one — it swallowed
# Shopee's "Shipping Fee Paid by Buyer", and only the row identity caught it.
_WIDE_META = [
    "order id", "order no", "order sn", "order number", "order item no",
    "date", "time", "status", "statement", "payment ref", "ref id", "currency",
    "buyer name", "buyer username", "seller sku", "product name", "tracking",
    "item name", "comment", "note", "transaction number", "payment ref id",
]

# The stated total a wide row's components must sum to.
_WIDE_TOTAL = [
    "final amount", "grand total", "total released", "net payout", "payout amount",
    "total amount", "settlement amount",
]

# Rows that state what the platform paid out. These are not settlement lines —
# they are the figure Fynn reconciles the settlement lines against, so folding
# them into the total would make every cycle tie out trivially and wrongly.
PAYOUT_LABELS = {
    "payout", "total payout", "settlement", "total settlement amount", "withdrawal",
    "released amount", "amount released", "bank transfer", "net payout", "paid out",
    "grand total",
}

# Trailing parentheticals that name a currency or a country's tax, stripped so
# one rule covers "Product Service Tax (GST)" in Malaysia and the same column
# labelled (VAT) or (SST) elsewhere.
_SUFFIX_CODES = {
    "gst", "vat", "sst", "wht", "myr", "sgd", "idr", "php", "thb", "vnd", "usd", "rm", "s$",
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
    "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y",
)


@dataclass
class ParsedSettlement:
    """Everything one uploaded file yielded."""

    lines: list[SettlementLine] = field(default_factory=list)
    reported_payouts: dict[Platform, float] = field(default_factory=dict)
    cycles: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    layout: str = "long"

    @property
    def cycle(self) -> str:
        """The dominant cycle in the file, for labelling the close."""
        if not self.cycles:
            return "unknown"
        return sorted(self.cycles)[-1]

    @property
    def platforms(self) -> set[Platform]:
        return {l.platform for l in self.lines}


# ── Reading ───────────────────────────────────────────────────────────────────

def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _is_xlsx(raw: bytes, filename: str) -> bool:
    # xlsx is a zip; the magic bytes are a cheaper and more reliable signal than
    # the extension, which an upload does not always preserve.
    return raw[:2] == b"PK" or filename.lower().endswith((".xlsx", ".xlsm"))


def _rows_from_xlsx(raw: bytes) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise SettlementParseError(
            "That looks like an Excel file, but openpyxl is not installed. "
            "Export it as CSV instead."
        )
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise SettlementParseError(f"Could not open that Excel file: {exc}")

    rows: list[list[str]] = []
    for row in wb[wb.sheetnames[0]].iter_rows(values_only=True):
        rows.append(["" if c is None else str(c).strip() for c in row])
    wb.close()
    return rows


def _rows_from_csv(raw: bytes) -> list[list[str]]:
    text = _decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # a single-column file, or one Sniffer cannot read
    return [[(c or "").strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def _blocks(rows: list[list[str]]) -> list[list[dict]]:
    """Split a sheet into blocks, each with its own header row.

    Whether a platform ships adjustments as a separate file or as a second
    section of the same one is not verified, so both are handled: a blank line
    followed by a row that looks like a new header starts a new block.
    """
    blocks: list[list[dict]] = []
    header: Optional[list[str]] = None
    current: list[dict] = []

    def flush():
        if header and current:
            blocks.append(current.copy())
        current.clear()

    for row in rows:
        if not any(cell for cell in row):          # blank separator line
            flush()
            header = None
            continue
        if header is None:
            header = [c for c in row]
            continue
        # A row whose cells are all non-numeric where the header expects numbers
        # is a new header, not data — the usual shape of an appended section.
        if _looks_like_header(header, row):
            flush()
            header = [c for c in row]
            continue
        current.append({header[i] if i < len(header) else f"col{i}": v
                        for i, v in enumerate(row)})
    flush()
    return blocks


def _looks_like_header(header: list[str], row: list[str]) -> bool:
    """True when `row` reads as a fresh header rather than data under `header`."""
    if len(row) < 2:
        return False
    cells = [c for c in row if c]
    if not cells:
        return False
    if any(_parse_amount(c) is not None for c in cells):
        return False  # contains a number: it is data
    # Every cell is text. Treat as a header only if it differs from the current
    # one — a text-only data row (all zeros stripped, say) should not split.
    return [c.strip().lower() for c in row] != [c.strip().lower() for c in header]


# ── Value helpers ─────────────────────────────────────────────────────────────

def _parse_amount(raw: str, decimal: str = ".") -> Optional[float]:
    """Strip currency symbols, thousands separators and bracketed negatives.

    `decimal` says which character this file uses as the decimal point, because
    that is not universal across the markets these platforms serve. Indonesia
    and Vietnam write 2.861 for two thousand eight hundred and sixty-one; read
    with the wrong convention it becomes 2.861, a thousandfold understatement
    that no later check would catch on a long-format file. See
    _detect_decimal_separator for how the convention is established.
    """
    text = (raw or "").strip()
    if not text:
        return None
    negative = (text.startswith("(") and text.endswith(")")) or text.lstrip().startswith("-")

    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None

    thousands = "," if decimal == "." else "."
    cleaned = cleaned.replace(thousands, "")
    if decimal != ".":
        cleaned = cleaned.replace(decimal, ".")
    if cleaned.count(".") > 1 or cleaned in ("", "."):
        return None

    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _detect_decimal_separator(rows: list[list[str]], sample: int = 400) -> str:
    """Work out whether this file writes 1.234,56 or 1,234.56.

    Decided on evidence from the file itself, never assumed:

      a value containing both separators settles it — the last one is the
      decimal point, as in 1.234,56 and 1,234.56 alike;

      otherwise a separator followed by one or two digits is a decimal point
      (853.59), while one followed by exactly three digits is a thousands
      grouping (2.861). Money is written to at most two places, so three digits
      after a separator is a group, not a fraction.

    Where a file offers no evidence either way, '.' is assumed and the caller is
    warned rather than left to find out from a wrong total.
    """
    both = {".": 0, ",": 0}
    fraction = {".": 0, ",": 0}
    grouping = {".": 0, ",": 0}

    for row in rows[:sample]:
        for cell in row:
            text = re.sub(r"[^\d.,]", "", (cell or "").strip())
            if not text or not any(c.isdigit() for c in text):
                continue
            last_dot, last_comma = text.rfind("."), text.rfind(",")
            if last_dot >= 0 and last_comma >= 0:
                both["." if last_dot > last_comma else ","] += 1
                continue
            for sep, last in ((".", last_dot), (",", last_comma)):
                if last < 0:
                    continue
                after = len(text) - last - 1
                if text.count(sep) > 1 or after == 3:
                    grouping[sep] += 1
                elif 1 <= after <= 2:
                    fraction[sep] += 1

    if both["."] or both[","]:
        return "." if both["."] >= both[","] else ","
    if fraction["."] or fraction[","]:
        return "." if fraction["."] >= fraction[","] else ","
    if grouping["."] and not grouping[","]:
        return ","   # every dot is a thousands group, so the decimal point is a comma
    if grouping[","] and not grouping["."]:
        return "."
    return "."


def _clean_label(header: str) -> str:
    """A fee column's header, minus any currency or tax-code suffix."""
    label = (header or "").strip()
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", label)
    if match and match.group(2).strip().lower() in _SUFFIX_CODES:
        return match.group(1).strip()
    return label


def _normalise_cycle(value: str) -> Optional[str]:
    """Reduce a period or date cell to YYYY-MM."""
    from datetime import datetime

    text = (value or "").strip()
    if not text:
        return None
    # Lazada states the statement period as a range — "14 Dec 2020 - 20 Dec
    # 2020" — because it settles weekly, not monthly. The period is named by
    # where it starts.
    span = re.split(r"\s+(?:-|–|—|to)\s+", text, maxsplit=1)
    if len(span) == 2 and any(c.isdigit() for c in span[1]):
        text = span[0].strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    match = re.match(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return None


def cycle_from_filename(filename: str) -> Optional[str]:
    """Pull the period out of a filename like shopee-income-statement-2026-01.csv.

    A settlement file *is* a period. Rows inside it routinely carry dates from
    the next month — an order released on 2026-02-01 still belongs to the
    January statement it was paid in — so the filename outranks the row date,
    and only an explicit statement column outranks the filename.
    """
    name = (filename or "").lower()
    match = re.search(r"(20\d{2})[-_/ ]?(0[1-9]|1[0-2])(?!\d)", name)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    months = ("january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december")
    for index, month in enumerate(months, start=1):
        for form in (month, month[:3]):
            hit = re.search(rf"{form}[-_ ]?(20\d{{2}})", name)
            if hit:
                return f"{hit.group(1)}-{index:02d}"
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


def platform_from_content(rows: list[list[str]], sample: int = 60) -> Optional[Platform]:
    """Identify the platform from what the file says about itself.

    A filename is not always informative — a download may arrive as
    export(3).csv, or renamed entirely. The contents still name the platform:
    Shopee's statement has "Shopee Discount" and "Shopee Coins Redeemed" as
    column headers, and Lazada's classifies lines under "Orders-Lazada Fees".

    Requires a clear winner — a file that mentions two marketplaces is
    ambiguous, and guessing which one owns the money is not this module's call.
    """
    counts: dict[Platform, int] = {}
    for row in rows[:sample]:
        for cell in row:
            hit = platform_from_text(cell)
            if hit is not None:
                counts[hit] = counts.get(hit, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def platform_from_known_orders(
    rows: list[list[str]], known_orders: dict[str, Platform], sample: int = 400
) -> Optional[Platform]:
    """Identify the platform by the orders the file refers to.

    An adjustments file names no marketplace anywhere — not in its filename, its
    columns or its contents — but its order numbers are the ones already sitting
    in the open cycle. Matching against those attributes the file without
    guessing. Requires a clear winner, as the content sniff does.
    """
    if not known_orders:
        return None
    counts: dict[Platform, int] = {}
    for row in rows[:sample]:
        for cell in row:
            platform = known_orders.get((cell or "").strip())
            if platform is not None:
                counts[platform] = counts.get(platform, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _dominant_cycle(rows: list[list[str]], sample: int = 400) -> Optional[str]:
    """The period most of the file's dates fall in.

    A settlement file is one period. When neither a statement column nor the
    filename says which, the rows themselves decide — and the majority wins, so
    that the handful of lines a statement always carries into the next month do
    not split one close in two.
    """
    counts: dict[str, int] = {}
    for row in rows[:sample]:
        for cell in row:
            cycle = _normalise_cycle(cell) if re.search(r"\d{4}", cell or "") else None
            if cycle:
                counts[cycle] = counts.get(cycle, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


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


# ── Column resolution ─────────────────────────────────────────────────────────

def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our field names onto the file's actual headers."""
    headers = {(h or "").strip().lower(): h for h in fieldnames if h}
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    for field_name, aliases in _COLUMNS.items():
        for alias in aliases:
            if alias in headers and headers[alias] not in taken:
                resolved[field_name] = headers[alias]
                taken.add(headers[alias])
                break
        else:
            for alias in aliases:
                match = next(
                    (h for h in headers if alias in h and headers[h] not in taken), None
                )
                if match:
                    resolved[field_name] = headers[match]
                    taken.add(headers[match])
                    break
    return resolved


def _matches_any(header: str, needles: list[str]) -> bool:
    lowered = (header or "").strip().lower()
    return any(n in lowered for n in needles)


def _is_wide(fieldnames: list[str], cols: dict[str, str]) -> bool:
    """A wide file has a total column and several unnamed fee columns."""
    if "label" in cols:
        return False
    total = [h for h in fieldnames if _matches_any(h, _WIDE_TOTAL)]
    if not total:
        return False
    fees = [
        h for h in fieldnames
        if h and h not in total and not _matches_any(h, _WIDE_META)
    ]
    return len(fees) >= 3


# ── Melting ───────────────────────────────────────────────────────────────────

def _melt_wide(
    rows: list[dict],
    fieldnames: list[str],
    filename: str,
    file_platform: Optional[Platform],
    file_cycle: Optional[str],
    default_cycle: Optional[str],
    parsed: ParsedSettlement,
    decimal: str = ".",
) -> None:
    """One row per order, fees as columns → one SettlementLine per fee."""
    total_col = next(h for h in fieldnames if _matches_any(h, _WIDE_TOTAL))
    cols = _resolve_columns(fieldnames)
    fee_cols = [
        h for h in fieldnames
        if h and h != total_col and not _matches_any(h, _WIDE_META)
    ]

    mismatches: list[str] = []
    for row_number, row in enumerate(rows, start=2):
        platform = (
            platform_from_text(row.get(cols["platform"], "")) if "platform" in cols else None
        ) or file_platform
        if platform is None:
            parsed.skipped.append(
                f"row {row_number}: no platform column, and the filename does not name one"
            )
            continue

        order_id = (row.get(cols["order"], "") or "").strip() if "order" in cols else ""
        date = (row.get(cols["date"], "") or "").strip() if "date" in cols else ""
        cycle = (row.get(cols["cycle"], "") or "").strip() if "cycle" in cols else ""
        cycle = (
            _normalise_cycle(cycle) or file_cycle
            or _normalise_cycle(date) or default_cycle or "unknown"
        )

        components = 0.0
        for header in fee_cols:
            amount = _parse_amount(row.get(header, ""), decimal)
            if amount is None:
                continue
            components += amount
            if abs(amount) < 0.005:
                continue  # a zero fee is not a line worth posting
            parsed.lines.append(
                SettlementLine(
                    platform=platform, cycle=cycle, label=_clean_label(header),
                    amount=amount, order_id=order_id or None, date=date or None,
                    source_ref=filename,
                )
            )
        parsed.cycles.add(cycle)

        stated = _parse_amount(row.get(total_col, ""), decimal)
        if stated is not None and abs(round(components - stated, 2)) >= 0.005:
            mismatches.append(
                f"{order_id or f'row {row_number}'}: components {components:.2f} "
                f"vs stated {stated:.2f}"
            )

    if mismatches:
        # Do not reconcile a file whose own arithmetic does not hold. It means a
        # column was read as meta when it carries value, and the difference
        # would resurface later as a residual nobody can explain.
        raise SettlementParseError(
            f"{len(mismatches)} row(s) do not sum to their stated {total_col}. "
            f"First: {mismatches[0]}. The column layout is probably not what Fynn expects."
        )


def _melt_long(
    rows: list[dict],
    fieldnames: list[str],
    filename: str,
    file_platform: Optional[Platform],
    file_cycle: Optional[str],
    default_cycle: Optional[str],
    parsed: ParsedSettlement,
    decimal: str = ".",
) -> None:
    """One row per fee line → one SettlementLine each."""
    cols = _resolve_columns(fieldnames)
    missing = [f for f in ("label",) if f not in cols]
    if "amount" not in cols and not ("credit" in cols or "debit" in cols):
        missing.append("amount")
    if missing:
        raise SettlementParseError(
            "Missing required column(s): " + ", ".join(missing)
            + ". Detected headers: " + ", ".join(h for h in fieldnames[:12] if h)
        )

    for row_number, row in enumerate(rows, start=2):
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
            amount = _parse_amount(row.get(cols["amount"], ""), decimal)
        else:
            credit = _parse_amount(row.get(cols.get("credit", ""), ""), decimal) or 0.0
            debit = _parse_amount(row.get(cols.get("debit", ""), ""), decimal) or 0.0
            amount = credit - abs(debit)
        if amount is None:
            parsed.skipped.append(f"row {row_number}: unreadable amount")
            continue

        date = (row.get(cols["date"], "") or "").strip() if "date" in cols else ""
        cycle_cell = (row.get(cols["cycle"], "") or "").strip() if "cycle" in cols else ""
        # The statement period wins over the transaction date: a statement often
        # carries a few lines dated into the next month, and letting those name
        # their own cycle would split one close in two.
        cycle = (
            _normalise_cycle(cycle_cell) or file_cycle
            or _normalise_cycle(date) or default_cycle or "unknown"
        )

        if label.strip().lower() in PAYOUT_LABELS:
            # Stated positive; a withdrawal row is often negative because it
            # leaves the platform balance.
            parsed.reported_payouts[platform] = abs(amount)
            parsed.cycles.add(cycle)
            continue

        order_id = (row.get(cols["order"], "") or "").strip() if "order" in cols else ""
        category = (row.get(cols["category"], "") or "").strip() if "category" in cols else ""
        note = (row.get(cols["note"], "") or "").strip() if "note" in cols else ""
        parsed.lines.append(
            SettlementLine(
                platform=platform, cycle=cycle, label=label, amount=amount,
                order_id=order_id or None, date=date or None, source_ref=filename,
                category=category or None, note=note or None,
            )
        )
        parsed.cycles.add(cycle)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_settlement_csv(
    raw: bytes,
    filename: str = "upload.csv",
    default_platform: Optional[Platform] = None,
    default_cycle: Optional[str] = None,
    known_orders: Optional[dict[str, Platform]] = None,
) -> ParsedSettlement:
    """Read one settlement export into SettlementLines.

    The platform is taken from a column when the file has one, then the
    filename, then the file's own contents, then the orders it refers to, then
    `default_platform`. A file that resolves to no platform at all is rejected
    rather than guessed at.
    """
    # Blank rows are kept: they are how a second section announces itself.
    rows = _rows_from_xlsx(raw) if _is_xlsx(raw, filename) else _rows_from_csv(raw)
    if not any(any(c for c in row) for row in rows):
        raise SettlementParseError("That file is empty.")

    blocks = _blocks(rows)
    if not blocks:
        raise SettlementParseError("That file has a header but no rows.")

    # Filename first, then what the file says about itself, then the caller's
    # default. The content sniff is what makes an unhelpfully named file —
    # export(3).csv — resolve to a platform at all.
    file_platform = (
        platform_from_text(filename)
        or platform_from_content(rows)
        or platform_from_known_orders(rows, known_orders or {})
        or default_platform
    )
    file_cycle = cycle_from_filename(filename) or _dominant_cycle(rows)
    decimal = _detect_decimal_separator(rows)
    parsed = ParsedSettlement()
    if decimal != ".":
        parsed.warnings.append(
            f"Amounts read with '{decimal}' as the decimal point (this file writes 1.234,56)."
        )

    for block in blocks:
        fieldnames = list(block[0].keys())
        cols = _resolve_columns(fieldnames)
        if _is_wide(fieldnames, cols):
            parsed.layout = "wide"
            _melt_wide(block, fieldnames, filename, file_platform, file_cycle, default_cycle, parsed, decimal)
        else:
            _melt_long(block, fieldnames, filename, file_platform, file_cycle, default_cycle, parsed, decimal)

    if not parsed.lines:
        if file_platform is None:
            raise SettlementParseError(
                "I can't tell which marketplace that file is from. Nothing in the "
                "filename, the columns or the contents names one.\n\n"
                "Rename it to include the platform — shopee-jan.csv — or send it "
                "from the upload page, where you can pick."
            )
        detail = f" ({parsed.skipped[0]})" if parsed.skipped else ""
        raise SettlementParseError(f"No settlement lines could be read from that file{detail}.")

    if parsed.skipped:
        parsed.warnings.append(f"{len(parsed.skipped)} row(s) skipped")
    return parsed
