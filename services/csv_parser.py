"""
CSV parsers for Fynn — Shopee and Lazada.

Shopee MY Finance CSV (Finance → My Income → Export):
  Transaction Date | Type | Order SN | Amount | Status

Lazada MY Statement CSV (Finance → Transactions → Export):
  Created At | Transaction Type | Order No. | Amount | Status
  OR: Date | Type | Order ID | Credit | Debit

Amount sign: positive = credit, negative = debit (both platforms).
"""

import csv
import io
import re
from datetime import datetime
from typing import List

from models.transaction import Transaction, TransactionType


# Map Shopee type strings → our TransactionType
_TYPE_MAP = {
    "buyer payment":         TransactionType.ORDER,
    "order income":          TransactionType.ORDER,
    "shopee commission":     TransactionType.PLATFORM_FEE,
    "transaction fee":       TransactionType.PLATFORM_FEE,
    "commission fee":        TransactionType.PLATFORM_FEE,
    "service fee":           TransactionType.PLATFORM_FEE,
    "refund to buyer":       TransactionType.REFUND,
    "return refund":         TransactionType.REFUND,
    "buyer refund":          TransactionType.REFUND,
    "shipping fee":          TransactionType.SHIPPING,
    "shipping rebate":       TransactionType.SHIPPING,
    "shipping subsidy":      TransactionType.SHIPPING,
    "reverse shipping fee":  TransactionType.SHIPPING,
    "voucher":               TransactionType.VOUCHER,
    "seller voucher":        TransactionType.VOUCHER,
    "shopee voucher":        TransactionType.VOUCHER,
    "withdrawal":            TransactionType.SETTLEMENT,
    "bank transfer":         TransactionType.SETTLEMENT,
    "payout":                TransactionType.SETTLEMENT,
    "settlement":            TransactionType.SETTLEMENT,
}


def _map_type(raw_type: str) -> TransactionType:
    key = raw_type.strip().lower()
    for pattern, txn_type in _TYPE_MAP.items():
        if pattern in key:
            return txn_type
    return TransactionType.OTHER


def _parse_amount(raw: str) -> float:
    """Strip currency symbols, commas, spaces and cast to float."""
    cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
    return float(cleaned) if cleaned else 0.0


def _parse_date(raw: str) -> datetime:
    """Try common Shopee date formats."""
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}")


def _find_column(headers: list[str], candidates: list[str]) -> str | None:
    """Return the first header that matches one of the candidate names (case-insensitive)."""
    lower = [h.strip().lower() for h in headers]
    for candidate in candidates:
        if candidate.lower() in lower:
            return headers[lower.index(candidate.lower())]
    return None


def parse_shopee_csv(content: bytes | str) -> List[Transaction]:
    """
    Parse a Shopee Finance CSV export into Transaction objects.

    Args:
        content: Raw CSV bytes or string from the uploaded file.

    Returns:
        List of Transaction objects sorted by date ascending.

    Raises:
        ValueError: If required columns are missing or no valid rows parsed.
    """
    if isinstance(content, bytes):
        # Try UTF-8 with BOM, fall back to latin-1 (Shopee sometimes uses this)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []

    # Locate required columns flexibly
    col_date   = _find_column(headers, ["Transaction Date", "Date", "Created Time", "Create Time"])
    col_type   = _find_column(headers, ["Type", "Transaction Type", "Description"])
    col_amount = _find_column(headers, ["Amount", "Total Amount", "Amount (MYR)", "Credit/Debit"])
    col_order  = _find_column(headers, ["Order SN", "Order ID", "Reference SN", "Reference ID"])
    col_status = _find_column(headers, ["Status", "Transaction Status"])

    if not col_date or not col_type or not col_amount:
        raise ValueError(
            f"CSV is missing required columns. Found: {headers}. "
            "Expected: Transaction Date, Type, Amount"
        )

    transactions: List[Transaction] = []
    skipped = 0

    for i, row in enumerate(reader):
        try:
            raw_type   = row.get(col_type, "").strip()
            raw_amount = row.get(col_amount, "").strip()
            raw_date   = row.get(col_date, "").strip()
            raw_order  = row.get(col_order, "").strip() if col_order else ""
            raw_status = row.get(col_status, "").strip() if col_status else ""

            # Skip header repeats, totals rows, or empty rows
            if not raw_amount or not raw_date or not raw_type:
                skipped += 1
                continue

            # Skip pending/cancelled transactions
            if raw_status.lower() in {"pending", "cancelled", "canceled", "failed"}:
                skipped += 1
                continue

            amount  = _parse_amount(raw_amount)
            date    = _parse_date(raw_date)
            txn_type = _map_type(raw_type)

            txn = Transaction(
                transaction_id=f"CSV-{i+1:04d}",
                order_id=raw_order or None,
                date=date,
                type=txn_type,
                description=raw_type,
                amount_myr=amount,
            )
            transactions.append(txn)

        except (ValueError, KeyError) as exc:
            skipped += 1
            print(f"  [CSV] Skipped row {i+1}: {exc}")
            continue

    if not transactions:
        raise ValueError("No valid transactions found in CSV. Check column names and data.")

    transactions.sort(key=lambda t: t.date)
    print(f"  [CSV] Parsed {len(transactions)} transactions ({skipped} skipped).")
    return transactions


# ── Lazada parser ──────────────────────────────────────────────────────────────

_LAZADA_TYPE_MAP = {
    # Specific entries must come before short overlapping ones
    # (matching is substring-based, so "payment" would match "payment fee" too)

    # Settlement / payout
    "lazwallet":              TransactionType.SETTLEMENT,
    "bank transfer":          TransactionType.SETTLEMENT,
    "transfer":               TransactionType.SETTLEMENT,
    "payout":                 TransactionType.SETTLEMENT,
    "settlement":             TransactionType.SETTLEMENT,

    # Platform fees — specific before generic
    "lazada commission":      TransactionType.PLATFORM_FEE,
    "payment fee":            TransactionType.PLATFORM_FEE,
    "transaction fee":        TransactionType.PLATFORM_FEE,
    "service fee":            TransactionType.PLATFORM_FEE,
    "commission":             TransactionType.PLATFORM_FEE,
    "marketing fee":          TransactionType.PLATFORM_FEE,
    "sponsored":              TransactionType.PLATFORM_FEE,
    "storage fee":            TransactionType.PLATFORM_FEE,
    "late dispatch":          TransactionType.PLATFORM_FEE,

    # Shipping
    "shipping fee voucher":   TransactionType.SHIPPING,
    "shipping rebate":        TransactionType.SHIPPING,
    "shipping fee":           TransactionType.SHIPPING,
    "shipping":               TransactionType.SHIPPING,

    # Vouchers
    "seller voucher":         TransactionType.VOUCHER,
    "lazada voucher":         TransactionType.VOUCHER,
    "voucher":                TransactionType.VOUCHER,

    # Refunds
    "charge back":            TransactionType.REFUND,
    "chargeback":             TransactionType.REFUND,
    "reversal":               TransactionType.REFUND,
    "return":                 TransactionType.REFUND,
    "refund":                 TransactionType.REFUND,

    # Orders / income — "payment" last so it doesn't swallow "payment fee"
    "compensation":           TransactionType.ORDER,
    "cashback":               TransactionType.ORDER,
    "order income":           TransactionType.ORDER,
    "item price":             TransactionType.ORDER,
    "payment":                TransactionType.ORDER,
}


def _map_lazada_type(raw_type: str) -> TransactionType:
    key = raw_type.strip().lower()
    for pattern, txn_type in _LAZADA_TYPE_MAP.items():
        if pattern in key:
            return txn_type
    return TransactionType.OTHER


def parse_lazada_csv(content: bytes | str) -> List[Transaction]:
    """
    Parse a Lazada Finance statement CSV into Transaction objects.

    Handles both Credit/Debit split columns and a single Amount column.
    Lazada MY columns (typical export):
      Created At | Transaction Type | Order No. | Credit | Debit | Status
    Alternative layout:
      Date | Type | Order ID | Amount | Status

    Args:
        content: Raw CSV bytes or string.

    Returns:
        List of Transaction objects sorted by date ascending.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []

    col_date   = _find_column(headers, ["Created At", "Date", "Transaction Date", "Create Time"])
    col_type   = _find_column(headers, ["Transaction Type", "Type", "Description", "Remarks"])
    col_order  = _find_column(headers, ["Order No.", "Order No", "Order ID", "Order Number", "Reference No."])
    col_credit = _find_column(headers, ["Credit", "Credit (MYR)", "Credit Amount"])
    col_debit  = _find_column(headers, ["Debit", "Debit (MYR)", "Debit Amount"])
    col_amount = _find_column(headers, ["Amount", "Total Amount", "Amount (MYR)"])
    col_status = _find_column(headers, ["Status", "Transaction Status"])

    if not col_date or not col_type:
        raise ValueError(
            f"Lazada CSV missing required columns. Found: {headers}. "
            "Expected: Created At / Date, Transaction Type / Type"
        )

    # Must have either split credit/debit OR a single amount column
    if not col_amount and not (col_credit or col_debit):
        raise ValueError(
            f"Lazada CSV missing amount column. Found: {headers}. "
            "Expected: Amount OR Credit + Debit"
        )

    transactions: List[Transaction] = []
    skipped = 0

    for i, row in enumerate(reader):
        try:
            raw_type   = row.get(col_type, "").strip()
            raw_date   = row.get(col_date, "").strip()
            raw_order  = row.get(col_order, "").strip() if col_order else ""
            raw_status = row.get(col_status, "").strip() if col_status else ""

            if not raw_date or not raw_type:
                skipped += 1
                continue

            if raw_status.lower() in {"pending", "cancelled", "canceled", "failed", "processing"}:
                skipped += 1
                continue

            # Resolve amount: prefer Credit − Debit, fall back to Amount
            if col_credit or col_debit:
                credit = _parse_amount(row.get(col_credit, "0") or "0")
                debit  = _parse_amount(row.get(col_debit, "0") or "0")
                # Lazada debits are usually stored as positive numbers
                amount = credit - abs(debit)
            else:
                raw_amount = row.get(col_amount, "").strip()
                if not raw_amount:
                    skipped += 1
                    continue
                amount = _parse_amount(raw_amount)

            date     = _parse_date(raw_date)
            txn_type = _map_lazada_type(raw_type)

            transactions.append(Transaction(
                transaction_id=f"LAZ-{i+1:04d}",
                order_id=raw_order or None,
                date=date,
                type=txn_type,
                description=raw_type,
                amount_myr=amount,
            ))

        except (ValueError, KeyError) as exc:
            skipped += 1
            print(f"  [CSV/Lazada] Skipped row {i+1}: {exc}")
            continue

    if not transactions:
        raise ValueError("No valid Lazada transactions found. Check column names and data.")

    transactions.sort(key=lambda t: t.date)
    print(f"  [CSV/Lazada] Parsed {len(transactions)} transactions ({skipped} skipped).")
    return transactions


# ── Cost period detector ───────────────────────────────────────────────────────

_DATE_COL_CANDIDATES = [
    "Date", "Invoice Date", "Month", "Created At", "Transaction Date",
    "Create Time", "Payment Date", "Period",
]


def detect_cost_period(content: bytes | str) -> str:
    """
    Detect the dominant month/year from a cost CSV's date column.

    Tries common date column names, parses every date, and returns the
    month that appears most frequently as "March 2026". Falls back to
    "unknown" if no date column is found or dates can't be parsed.

    Args:
        content: Raw CSV bytes or string.

    Returns:
        Period string e.g. "March 2026", or "unknown".
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    col = _find_column(headers, _DATE_COL_CANDIDATES)
    if not col:
        return "unknown"

    from collections import Counter
    month_counts: Counter = Counter()

    for row in reader:
        raw = (row.get(col) or "").strip()
        if not raw:
            continue
        # Try "March 2026" / "March" style first (payroll Month column)
        for fmt in ("%B %Y", "%b %Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                month_counts[dt.strftime("%B %Y")] += 1
                break
            except ValueError:
                continue
        else:
            # Try full date formats
            try:
                dt = _parse_date(raw)
                month_counts[dt.strftime("%B %Y")] += 1
            except ValueError:
                continue

    if not month_counts:
        return "unknown"

    period = month_counts.most_common(1)[0][0]
    return period


# ── Generic cost total parser ──────────────────────────────────────────────────

# Priority-ordered column name candidates per cost type
_COST_AMOUNT_COLS: dict[str, list[str]] = {
    "cogs":      ["Total Cost (MYR)", "Total (MYR)", "Amount (MYR)", "Total Cost", "Amount"],
    "ads":       ["Ad Spend (MYR)", "Spend (MYR)", "Amount (MYR)", "Ad Spend", "Spend", "Amount"],
    "warehouse": ["Amount (MYR)", "Total (MYR)", "Amount", "Total"],
    "payroll":   ["Total Cost (MYR)", "Amount (MYR)", "Total Cost", "Amount"],
    "packaging": ["Total (MYR)", "Total Cost (MYR)", "Amount (MYR)", "Total", "Amount"],
    "expense":   ["Amount (MYR)", "Total (MYR)", "Amount", "Total"],
}
_GENERIC_AMOUNT_COLS = ["Amount (MYR)", "Total (MYR)", "Total Cost (MYR)", "Amount", "Total", "Cost"]


def parse_cost_total(content: bytes | str, file_type: str) -> float:
    """
    Sum the total MYR amount from any cost CSV.

    Tries known column names for the given file_type, then falls back to
    a generic search. Returns 0.0 if no amount column can be found.

    Args:
        content:   Raw CSV bytes or string.
        file_type: One of cogs, ads, warehouse, payroll, packaging, expense.

    Returns:
        Total MYR amount (sum of all positive numeric rows).
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])

    candidates = _COST_AMOUNT_COLS.get(file_type, []) + _GENERIC_AMOUNT_COLS
    col = _find_column(headers, candidates)

    if not col:
        print(f"  [CostParser] No amount column found in {file_type} CSV. Headers: {headers}")
        return 0.0

    total = 0.0
    for row in reader:
        try:
            val = _parse_amount(row.get(col, "") or "")
            if val > 0:
                total += val
        except (ValueError, TypeError):
            continue

    print(f"  [CostParser] {file_type}: summed {col} → MYR {total:,.2f}")
    return round(total, 2)


# ── TikTok Shop parser ─────────────────────────────────────────────────────────

# ── TikTok Shop parser ─────────────────────────────────────────────────────────
#
# TikTok Shop exports a multi-sheet Excel workbook (.xlsx) from:
#   Seller Centre → Finance → Statements → Export
#
# Post-2024 workbook structure (5 sheets):
#   "Order details"  — one row per settled order, fee components as columns
#   "Shipping"       — shipping fee details
#   "Fees"           — platform fee breakdowns
#   "Adjustments"    — chargebacks, manual adjustments
#   "Reserve Details"— held/released reserve amounts
#
# Each Order details row synthesises into multiple Transaction objects:
#   Gross Sales          → ORDER
#   Referral Fee +
#     Transaction Fee +
#     Affiliate Commission → PLATFORM_FEE
#   Shipping Fee -
#     Shipping Incentive → SHIPPING
#   Seller Discount +
#     Platform Voucher   → VOUCHER
#   Refund Amount        → REFUND
#
# The net settlement (sum of Net Income column) becomes a single SETTLEMENT txn.
#
# Fallback: if the file is a plain CSV transaction-list (older format or
# Finance → Income Details export), the CSV path handles it row-by-row.

_TIKTOK_ORDER_SHEET_NAMES = [
    "order details", "orders", "order detail", "order", "settlement details",
    "income details", "transaction details",
]
_TIKTOK_FEE_SHEET_NAMES   = ["fees", "fee", "platform fees"]
_TIKTOK_ADJ_SHEET_NAMES   = ["adjustments", "adjustment", "chargeback"]
_TIKTOK_SHIP_SHEET_NAMES  = ["shipping", "shipping details"]


def _xlsx_sheet(wb, candidates: list[str]):
    """Return the first worksheet whose name matches one of the candidates (case-insensitive)."""
    names_lower = {ws.title.strip().lower(): ws for ws in wb.worksheets}
    for c in candidates:
        if c in names_lower:
            return names_lower[c]
    return None


def _xlsx_rows_as_dicts(ws) -> list[dict]:
    """Convert an openpyxl worksheet to a list of dicts keyed by header row."""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    # Find the first non-empty row as the header
    header_idx = 0
    for i, row in enumerate(rows):
        if any(cell is not None and str(cell).strip() for cell in row):
            header_idx = i
            break
    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    result = []
    for row in rows[header_idx + 1:]:
        if not any(cell is not None for cell in row):
            continue
        result.append({headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))})
    return result


def _safe_float(val) -> float:
    """Cast a cell value to float, returning 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _parse_tiktok_xlsx(content: bytes) -> List[Transaction]:
    """
    Parse a TikTok Shop settlement Excel workbook (.xlsx).

    Reads the Order details sheet (primary) and synthesises Transaction objects
    from per-order column values. Falls back to other sheets for fee/adjustment
    data if the Order details sheet is absent.
    """
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    transactions: List[Transaction] = []

    # ── Order details sheet (primary) ─────────────────────────────────────────
    ws_orders = _xlsx_sheet(wb, _TIKTOK_ORDER_SHEET_NAMES)

    if ws_orders:
        rows = _xlsx_rows_as_dicts(ws_orders)
        headers = list(rows[0].keys()) if rows else []

        col_date    = _find_column(headers, ["Statement Date", "Settlement Date", "Date", "Order Date", "Created At"])
        col_order   = _find_column(headers, ["Order ID", "Order No.", "Order No", "Order Number"])
        col_gross   = _find_column(headers, ["Gross Sales", "Revenue", "Sale Amount", "Order Amount", "Item Total", "Subtotal"])
        col_refund  = _find_column(headers, ["Refund Amount", "Gross Sales Refund", "Return Refund", "Refund"])
        col_ref_fee = _find_column(headers, ["Referral Fee", "Commission Fee", "Platform Commission", "Commission"])
        col_aff_fee = _find_column(headers, ["Affiliate Commission", "Creator Commission", "Affiliate Fee", "Affiliate"])
        col_txn_fee = _find_column(headers, ["Transaction Fee", "Payment Fee", "Service Fee"])
        col_ship    = _find_column(headers, ["Shipping Fee", "Customer Shipping", "Freight", "Shipping"])
        col_ship_sub= _find_column(headers, ["TikTok Shop Shipping Incentive", "Shipping Subsidy", "Shipping Incentive", "Shipping Rebate"])
        col_sel_dis = _find_column(headers, ["Seller Discount", "Seller Voucher", "Voucher Discount"])
        col_plt_vch = _find_column(headers, ["Platform Voucher", "TikTok Voucher", "Platform Promotion"])
        col_net     = _find_column(headers, ["Net Income", "Net Settlement Amount", "Net Payout", "Settlement Amount", "Net Amount"])
        col_status  = _find_column(headers, ["Status", "Order Status", "Settlement Status"])

        if not col_date or not col_gross:
            raise ValueError(
                f"TikTok Shop Excel 'Order details' sheet missing key columns. "
                f"Found: {headers[:10]}... Expected: Statement Date + Gross Sales"
            )

        total_net = 0.0

        for i, row in enumerate(rows):
            try:
                raw_date   = str(row.get(col_date) or "").strip()
                raw_order  = str(row.get(col_order) or "").strip() if col_order else ""
                raw_status = str(row.get(col_status) or "").strip().lower() if col_status else ""

                if not raw_date:
                    continue
                if raw_status in {"pending", "cancelled", "canceled", "failed", "processing"}:
                    continue

                date = _parse_date(raw_date)

                gross    = _safe_float(row.get(col_gross))
                refund   = abs(_safe_float(row.get(col_refund)))   if col_refund  else 0.0
                ref_fee  = abs(_safe_float(row.get(col_ref_fee)))  if col_ref_fee else 0.0
                aff_fee  = abs(_safe_float(row.get(col_aff_fee)))  if col_aff_fee else 0.0
                txn_fee  = abs(_safe_float(row.get(col_txn_fee)))  if col_txn_fee else 0.0
                ship     = _safe_float(row.get(col_ship))          if col_ship    else 0.0
                ship_sub = _safe_float(row.get(col_ship_sub))      if col_ship_sub else 0.0
                sel_dis  = abs(_safe_float(row.get(col_sel_dis)))  if col_sel_dis else 0.0
                plt_vch  = abs(_safe_float(row.get(col_plt_vch)))  if col_plt_vch else 0.0
                net      = _safe_float(row.get(col_net))           if col_net     else None

                order_id = raw_order or None
                pfx = f"TT-{i+1:04d}"

                if gross:
                    transactions.append(Transaction(
                        transaction_id=f"{pfx}-ORDER", order_id=order_id, date=date,
                        type=TransactionType.ORDER,
                        description="Gross Sales", amount_myr=gross,
                    ))
                if refund:
                    transactions.append(Transaction(
                        transaction_id=f"{pfx}-REFUND", order_id=order_id, date=date,
                        type=TransactionType.REFUND,
                        description="Refund", amount_myr=-refund,
                    ))
                platform_fees = ref_fee + aff_fee + txn_fee
                if platform_fees:
                    transactions.append(Transaction(
                        transaction_id=f"{pfx}-FEE", order_id=order_id, date=date,
                        type=TransactionType.PLATFORM_FEE,
                        description="Platform Fees (Referral + Transaction + Affiliate)",
                        amount_myr=-platform_fees,
                    ))
                net_shipping = ship - ship_sub
                if net_shipping:
                    transactions.append(Transaction(
                        transaction_id=f"{pfx}-SHIP", order_id=order_id, date=date,
                        type=TransactionType.SHIPPING,
                        description="Shipping Fee (net of subsidy)",
                        amount_myr=-net_shipping,
                    ))
                vouchers = sel_dis + plt_vch
                if vouchers:
                    transactions.append(Transaction(
                        transaction_id=f"{pfx}-VCHR", order_id=order_id, date=date,
                        type=TransactionType.VOUCHER,
                        description="Vouchers (Seller + Platform)",
                        amount_myr=-vouchers,
                    ))

                # Accumulate net for settlement synthesis
                if net is not None:
                    total_net += net
                else:
                    # Derive net if column missing
                    total_net += gross - refund - platform_fees - net_shipping - vouchers

            except Exception as exc:
                print(f"  [XLSX/TikTok] Skipped order row {i+1}: {exc}")
                continue

        # Synthesise a single SETTLEMENT transaction from total net income
        if total_net:
            # Use the latest date in the sheet as the settlement date
            last_date = max((t.date for t in transactions), default=datetime.now())
            transactions.append(Transaction(
                transaction_id="TT-SETTLEMENT",
                order_id=None,
                date=last_date,
                type=TransactionType.SETTLEMENT,
                description="Net Settlement (TikTok Shop)",
                amount_myr=round(total_net, 2),
            ))

    # ── Adjustments sheet (chargebacks not in Order details) ──────────────────
    ws_adj = _xlsx_sheet(wb, _TIKTOK_ADJ_SHEET_NAMES)
    if ws_adj:
        adj_rows = _xlsx_rows_as_dicts(ws_adj)
        headers  = list(adj_rows[0].keys()) if adj_rows else []
        col_date = _find_column(headers, ["Date", "Statement Date", "Adjustment Date"])
        col_amt  = _find_column(headers, ["Amount", "Adjustment Amount", "Net Amount"])
        col_type = _find_column(headers, ["Type", "Reason", "Adjustment Type", "Description"])

        for i, row in enumerate(adj_rows):
            try:
                raw_date = str(row.get(col_date) or "").strip() if col_date else ""
                if not raw_date:
                    continue
                date   = _parse_date(raw_date)
                amount = _safe_float(row.get(col_amt)) if col_amt else 0.0
                desc   = str(row.get(col_type) or "Adjustment").strip() if col_type else "Adjustment"
                if amount == 0:
                    continue
                txn_type = TransactionType.REFUND if amount < 0 else TransactionType.ORDER
                transactions.append(Transaction(
                    transaction_id=f"TT-ADJ-{i+1:04d}",
                    order_id=None, date=date,
                    type=txn_type,
                    description=desc, amount_myr=amount,
                ))
            except Exception as exc:
                print(f"  [XLSX/TikTok] Skipped adjustment row {i+1}: {exc}")
                continue

    wb.close()

    if not transactions:
        raise ValueError("No valid TikTok Shop transactions found in Excel workbook.")

    transactions.sort(key=lambda t: t.date)
    order_count = sum(1 for t in transactions if t.type == TransactionType.ORDER)
    print(f"  [XLSX/TikTok] Parsed {len(transactions)} transaction entries from {order_count} orders.")
    return transactions


def _parse_tiktok_csv_list(content: bytes | str) -> List[Transaction]:
    """
    Parse a TikTok Shop transaction-list CSV (Finance → Income Details).

    This is the simpler single-CSV format where each row is one transaction type
    (not one order). Used as fallback when the file is not an Excel workbook.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])

    col_date   = _find_column(headers, ["Statement Date", "Date", "Transaction Date", "Created At"])
    col_type   = _find_column(headers, ["Transaction Type", "Type", "Description"])
    col_order  = _find_column(headers, ["Order ID", "Order No.", "Order No", "Reference ID"])
    col_credit = _find_column(headers, ["Credit", "Credit (MYR)", "Credit Amount"])
    col_debit  = _find_column(headers, ["Debit", "Debit (MYR)", "Debit Amount"])
    col_amount = _find_column(headers, ["Amount", "Total Amount", "Amount (MYR)"])
    col_status = _find_column(headers, ["Status", "Transaction Status"])

    if not col_date or not col_type:
        raise ValueError(
            f"TikTok Shop CSV missing required columns. Found: {headers}. "
            "Expected: Date / Statement Date, Type / Transaction Type"
        )
    if not col_amount and not (col_credit or col_debit):
        raise ValueError("TikTok Shop CSV missing amount column. Expected: Amount OR Credit + Debit")

    _TIKTOK_TYPE_MAP = {
        "withdrawal": TransactionType.SETTLEMENT, "transfer": TransactionType.SETTLEMENT,
        "payout": TransactionType.SETTLEMENT, "settlement": TransactionType.SETTLEMENT,
        "transaction fee": TransactionType.PLATFORM_FEE, "commission fee": TransactionType.PLATFORM_FEE,
        "commission": TransactionType.PLATFORM_FEE, "service fee": TransactionType.PLATFORM_FEE,
        "advertisement": TransactionType.PLATFORM_FEE, "ads fee": TransactionType.PLATFORM_FEE,
        "shipping fee subsidy": TransactionType.SHIPPING, "shipping rebate": TransactionType.SHIPPING,
        "shipping fee": TransactionType.SHIPPING, "shipping": TransactionType.SHIPPING,
        "platform voucher": TransactionType.VOUCHER, "seller voucher": TransactionType.VOUCHER,
        "voucher": TransactionType.VOUCHER, "discount": TransactionType.VOUCHER,
        "return refund": TransactionType.REFUND, "refund": TransactionType.REFUND,
        "chargeback": TransactionType.REFUND, "reversal": TransactionType.REFUND,
        "seller income": TransactionType.ORDER, "order income": TransactionType.ORDER,
        "order settlement": TransactionType.ORDER, "item income": TransactionType.ORDER,
        "payment": TransactionType.ORDER,
    }

    def _map(raw: str) -> TransactionType:
        key = raw.strip().lower()
        for pattern, t in _TIKTOK_TYPE_MAP.items():
            if pattern in key:
                return t
        return TransactionType.OTHER

    transactions: List[Transaction] = []
    skipped = 0

    for i, row in enumerate(reader):
        try:
            raw_type   = row.get(col_type, "").strip()
            raw_date   = row.get(col_date, "").strip()
            raw_order  = row.get(col_order, "").strip() if col_order else ""
            raw_status = row.get(col_status, "").strip().lower() if col_status else ""

            if not raw_date or not raw_type:
                skipped += 1; continue
            if raw_status in {"pending", "cancelled", "canceled", "failed", "processing"}:
                skipped += 1; continue

            if col_credit or col_debit:
                credit = _parse_amount(row.get(col_credit, "0") or "0")
                debit  = _parse_amount(row.get(col_debit, "0") or "0")
                amount = credit - abs(debit)
            else:
                raw_amount = row.get(col_amount, "").strip()
                if not raw_amount:
                    skipped += 1; continue
                amount = _parse_amount(raw_amount)

            transactions.append(Transaction(
                transaction_id=f"TT-{i+1:04d}",
                order_id=raw_order or None,
                date=_parse_date(raw_date),
                type=_map(raw_type),
                description=raw_type,
                amount_myr=amount,
            ))
        except (ValueError, KeyError) as exc:
            skipped += 1
            print(f"  [CSV/TikTok] Skipped row {i+1}: {exc}")

    if not transactions:
        raise ValueError("No valid TikTok Shop transactions found in CSV.")

    transactions.sort(key=lambda t: t.date)
    print(f"  [CSV/TikTok] Parsed {len(transactions)} transactions ({skipped} skipped).")
    return transactions


def parse_tiktok(content: bytes) -> List[Transaction]:
    """
    Auto-detect TikTok Shop file format and parse accordingly.

    Tries Excel (.xlsx) first (settlement workbook), falls back to CSV
    (transaction-list format from Finance → Income Details).
    """
    # Excel magic bytes: PK\x03\x04 (ZIP-based format)
    if isinstance(content, bytes) and content[:4] == b"PK\x03\x04":
        print("  [TikTok] Detected Excel workbook — using multi-sheet parser.")
        return _parse_tiktok_xlsx(content)
    print("  [TikTok] Detected CSV — using transaction-list parser.")
    return _parse_tiktok_csv_list(content)


# ── Dispatch ────────────────────────────────────────────────────────────────────

# Default source currency per platform (MY market).
# Override by passing currency= explicitly if the platform operates in a different market.
PLATFORM_CURRENCIES: dict[str, str] = {
    "shopee":  "MYR",
    "lazada":  "MYR",
    "tiktok":  "MYR",
    "amazon":  "USD",
    "shopify": "USD",
}

# Currency codes we can recognise in CSV content
_KNOWN_CURRENCIES = {"MYR", "SGD", "USD", "THB", "CNY", "IDR", "PHP", "VND"}

# Currency symbols → ISO code
_CURRENCY_SYMBOLS = {
    "rm": "MYR",
    "s$": "SGD",
    r"s\$": "SGD",
    "rp": "IDR",
    "฿": "THB",
    "¥": "CNY",
    "$": "USD",
}


_AMOUNT_HEADER_KEYWORDS = {"amount", "credit", "debit", "total", "price", "payout", "settlement"}


def _detect_currency_from_csv(content: bytes | str, filename: str = "") -> str | None:
    """
    Detect source currency from CSV content and filename.

    Checks (in priority order):
      1. Amount-related column headers containing a currency code e.g. "Amount (SGD)", "Credit (MYR)"
      2. Values in amount-related columns only (first 20 rows) — avoids false positives from
         description or order ID fields containing currency-like strings
      3. Filename containing a country/currency hint (MY, SG, ID, TH, PH)

    Returns:
        ISO 4217 currency code or None if detection fails.
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    else:
        text = content

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])

    # 1. Check amount-related column headers for embedded currency codes
    # e.g. "Credit (MYR)", "Amount (SGD)", "Debit (MYR)"
    amount_cols = [
        h for h in headers
        if any(kw in h.strip().lower() for kw in _AMOUNT_HEADER_KEYWORDS)
    ]
    for h in amount_cols:
        for code in _KNOWN_CURRENCIES:
            if code in h.upper():
                return code

    # 2. Scan values in amount-related columns only (first 20 rows)
    rows = []
    try:
        for i, row in enumerate(reader):
            if i >= 20:
                break
            rows.append(row)
    except Exception:
        pass

    for row in rows:
        for col in amount_cols:
            val = (row.get(col) or "").strip()
            if not val:
                continue
            upper = val.upper()
            for code in _KNOWN_CURRENCIES:
                if code in upper:
                    return code
            lower = val.lower()
            for symbol, code in _CURRENCY_SYMBOLS.items():
                if lower.startswith(symbol):
                    return code

    # 3. Filename hints — e.g. "shopee_SG_march.csv", "lazada_MY_finance.csv"
    fname = filename.upper()
    hint_map = {
        "_SG_": "SGD", "-SG-": "SGD", "_SGD_": "SGD", "SGD": "SGD",
        "_MY_": "MYR", "-MY-": "MYR", "_MYR_": "MYR", "MYR": "MYR",
        "_ID_": "IDR", "-ID-": "IDR",
        "_TH_": "THB", "-TH-": "THB",
        "_PH_": "PHP", "-PH-": "PHP",
    }
    for hint, code in hint_map.items():
        if hint in fname:
            return code

    return None


def parse_csv(content: bytes | str, platform: str, currency: str | None = None, filename: str = "") -> List[Transaction]:
    """
    Route to the correct platform parser and stamp each transaction with the source currency.

    Currency resolution order:
      1. Explicit `currency` argument (highest priority)
      2. Auto-detected from CSV content / filename
      3. Platform default from PLATFORM_CURRENCIES
      4. "MYR" fallback

    Args:
        content:  Raw CSV bytes or string.
        platform: One of "shopee", "lazada", etc.
        currency: Override source currency. If None, auto-detection runs first.
        filename: Original filename — used as a hint for currency detection.

    Returns:
        List of Transaction objects with currency set.
    """
    if currency:
        src_currency = currency
    else:
        detected = _detect_currency_from_csv(content, filename)
        src_currency = detected or PLATFORM_CURRENCIES.get(platform.lower(), "MYR")
        if detected:
            print(f"  [CSV] Auto-detected currency: {src_currency}")
        else:
            print(f"  [CSV] Currency not detected — using platform default: {src_currency}")

    if platform.lower() == "lazada":
        txns = parse_lazada_csv(content)
    elif platform.lower() == "tiktok":
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        txns = parse_tiktok(raw)
    else:
        txns = parse_shopee_csv(content)

    for t in txns:
        t.currency = src_currency

    return txns
