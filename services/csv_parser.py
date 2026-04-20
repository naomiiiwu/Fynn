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
    "payment":              TransactionType.ORDER,
    "item price":           TransactionType.ORDER,
    "order income":         TransactionType.ORDER,
    "cashback":             TransactionType.ORDER,
    "refund":               TransactionType.REFUND,
    "return":               TransactionType.REFUND,
    "reversal":             TransactionType.REFUND,
    "commission":           TransactionType.PLATFORM_FEE,
    "service fee":          TransactionType.PLATFORM_FEE,
    "payment fee":          TransactionType.PLATFORM_FEE,
    "transaction fee":      TransactionType.PLATFORM_FEE,
    "lazada commission":    TransactionType.PLATFORM_FEE,
    "shipping fee":         TransactionType.SHIPPING,
    "shipping":             TransactionType.SHIPPING,
    "shipping rebate":      TransactionType.SHIPPING,
    "voucher":              TransactionType.VOUCHER,
    "seller voucher":       TransactionType.VOUCHER,
    "lazada voucher":       TransactionType.VOUCHER,
    "transfer":             TransactionType.SETTLEMENT,
    "payout":               TransactionType.SETTLEMENT,
    "lazwallet":            TransactionType.SETTLEMENT,
    "bank transfer":        TransactionType.SETTLEMENT,
    "settlement":           TransactionType.SETTLEMENT,
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


# ── Dispatch ────────────────────────────────────────────────────────────────────

def parse_csv(content: bytes | str, platform: str) -> List[Transaction]:
    """
    Route to the correct platform parser.

    Args:
        content:  Raw CSV bytes or string.
        platform: One of "shopee", "lazada", or anything else (falls back to Shopee parser).

    Returns:
        List of Transaction objects.
    """
    if platform.lower() == "lazada":
        return parse_lazada_csv(content)
    return parse_shopee_csv(content)
