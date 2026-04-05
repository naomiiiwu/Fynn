"""
Claude-powered CSV file classifier for Fynn.

Given a CSV file's headers + first few rows, Claude identifies:
  - platform: shopee | lazada | amazon | shopify | tiktok | expense | unknown
  - file_type: transactions | cogs | ads | warehouse | payroll | packaging | expense | unknown
  - confidence: 0.0 – 1.0
  - notes: human-readable explanation

This lets sellers dump any CSV and Fynn figures out what it is.
"""

import csv
import io
import json
import os
import re
from dataclasses import dataclass

import anthropic


@dataclass
class ClassifiedFile:
    filename: str
    platform: str       # "shopee", "lazada", "amazon", "shopify", "tiktok", "generic", "unknown"
    file_type: str      # "transactions", "cogs", "ads", "warehouse", "payroll", "packaging", "expense", "unknown"
    confidence: float   # 0.0 – 1.0
    notes: str          # human-readable explanation
    headers: list[str]  # detected column headers


def _read_sample(content: bytes, max_rows: int = 5) -> tuple[list[str], list[dict]]:
    """Return headers and first N data rows from CSV bytes."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            break
        rows.append(dict(row))
    return headers, rows


def classify_file(filename: str, content: bytes) -> ClassifiedFile:
    """
    Use Claude to classify a CSV file by platform and type.

    Falls back to rule-based classification if no API key.

    Args:
        filename: Original filename (e.g. "shopee_finance_march.csv")
        content:  Raw CSV bytes

    Returns:
        ClassifiedFile with platform, file_type, confidence, notes.
    """
    headers, rows = _read_sample(content)

    # Try Claude first
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return _classify_with_claude(filename, headers, rows, api_key)

    # Fallback: rule-based
    return _classify_rule_based(filename, headers, rows)


VALID_PLATFORMS = {"shopee", "lazada", "amazon", "shopify", "tiktok", "generic", "unknown"}
VALID_FILE_TYPES = {"transactions", "cogs", "ads", "warehouse", "payroll", "packaging", "expense", "unknown"}


def _normalize_label(value: str, valid_values: set[str], default: str = "unknown") -> str:
    """Normalize free-form classifier output into one of our supported labels."""
    cleaned = re.sub(r"[^a-z]+", "", str(value).strip().lower())
    if cleaned in valid_values:
        return cleaned
    return default


def _combined_text(filename: str, headers: list[str], rows: list[dict]) -> str:
    """Flatten filename, headers, and sample row values into searchable text."""
    row_values: list[str] = []
    for row in rows:
        row_values.extend(str(value) for value in row.values() if value is not None)
    parts = [filename, *headers, *row_values]
    return " ".join(parts).lower()


def _infer_from_signals(filename: str, headers: list[str], rows: list[dict]) -> tuple[str, str, float]:
    """Deterministically infer platform and file type from the observed CSV content."""
    combined = _combined_text(filename, headers, rows)

    platform = "unknown"
    if any(token in combined for token in ["shopee", "buyer payment", "shopee commission", "seller voucher", "transaction fee"]):
        platform = "shopee"
    elif any(token in combined for token in ["lazada", "lazwallet", "lazpay"]):
        platform = "lazada"
    elif any(token in combined for token in ["amazon", "asin", "fba"]):
        platform = "amazon"
    elif any(token in combined for token in ["shopify", "shop pay"]):
        platform = "shopify"
    elif any(token in combined for token in ["tiktok", "tikshop", "tiktok shop"]):
        platform = "tiktok"

    file_type = "unknown"
    if any(token in combined for token in ["transaction", "buyer payment", "refund", "commission", "payout", "settlement", "withdrawal", "order income"]):
        file_type = "transactions"
    elif any(token in combined for token in ["supplier", "purchase", "cogs", "cost of goods", "invoice", "unit cost"]):
        file_type = "cogs"
    elif any(token in combined for token in ["ads", "advertising", "marketing", "campaign", "spend", "impression", "roas", "click"]):
        file_type = "ads"
    elif any(token in combined for token in ["warehouse", "storage", "fulfilment", "fulfillment", "3pl", "pick and pack"]):
        file_type = "warehouse"
    elif any(token in combined for token in ["payroll", "salary", "staff", "labour", "labor", "employee", "headcount"]):
        file_type = "payroll"
    elif any(token in combined for token in ["packaging", "package", "box", "poly", "mailer", "bubble wrap"]):
        file_type = "packaging"
    elif any(token in combined for token in ["expense", "cost", "fee", "overhead"]):
        file_type = "expense"

    if platform != "unknown" and file_type == "unknown":
        file_type = "transactions"

    if platform != "unknown" and file_type != "unknown":
        confidence = 0.95
    elif platform != "unknown" or file_type != "unknown":
        confidence = 0.8
    else:
        confidence = 0.3

    return platform, file_type, confidence


def _classify_with_claude(
    filename: str,
    headers: list[str],
    rows: list[dict],
    api_key: str,
) -> ClassifiedFile:
    """Ask Claude to classify the file."""
    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are classifying a CSV file for an e-commerce bookkeeping system.

Filename: {filename}
Headers: {headers}
Sample rows (first 5):
{json.dumps(rows, indent=2)}

Classify this file and respond with ONLY valid JSON in this exact format:
{{
  "platform": "shopee|lazada|amazon|shopify|tiktok|generic|unknown",
  "file_type": "transactions|cogs|ads|warehouse|payroll|packaging|expense|unknown",
  "confidence": 0.0,
  "notes": "brief explanation"
}}

Platform rules — check BOTH headers AND the actual cell values in sample rows:
- "shopee": any of "Shopee Commission", "Buyer Payment", "shopee" in filename, Type column contains Shopee-specific values
- "lazada": any of "Lazada", "LazWallet", "LazPay" anywhere in data
- "amazon": any of "Amazon", "ASIN", "FBA" anywhere in data
- "shopify": any of "Shopify", "Shop Pay" anywhere in data
- "tiktok": any of "TikTok", "TikShop" anywhere in data
- "generic": internal cost file with no platform branding (payroll, warehouse, supplier, etc.)
- "unknown": truly cannot determine after reading both headers and values

IMPORTANT: "Buyer Payment" and "Shopee Commission" as values in a Type/Description column = shopee platform.

File type rules:
- "transactions" = orders, refunds, platform fees, settlements, payouts — typical platform finance export
- "cogs"         = supplier invoices, unit cost, purchase orders, cost of goods
- "ads"          = ad spend, campaign, impressions, clicks, ROAS
- "warehouse"    = storage fees, fulfilment, 3PL, pick and pack
- "payroll"      = salary, staff, labour, employee, headcount
- "packaging"    = packaging, poly mailer, box, bubble wrap
- "expense"      = any other business cost

Be decisive. If the signals strongly point to a category, use confidence >= 0.85.
Only use "unknown" if you genuinely cannot tell after reading the data."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text.strip())
        inferred_platform, inferred_file_type, inferred_confidence = _infer_from_signals(filename, headers, rows)
        platform = _normalize_label(result.get("platform", "unknown"), VALID_PLATFORMS)
        file_type = _normalize_label(result.get("file_type", "unknown"), VALID_FILE_TYPES)
        confidence = float(result.get("confidence", 0.5))

        if platform == "unknown" and inferred_platform != "unknown":
            platform = inferred_platform
            confidence = max(confidence, inferred_confidence)
        if file_type == "unknown" and inferred_file_type != "unknown":
            file_type = inferred_file_type
            confidence = max(confidence, inferred_confidence)

        return ClassifiedFile(
            filename=filename,
            platform=platform,
            file_type=file_type,
            confidence=confidence,
            notes=result.get("notes", ""),
            headers=headers,
        )
    except Exception as exc:
        print(f"  [Classifier] Claude failed: {exc}, falling back to rules")
        return _classify_rule_based(filename, headers, rows)


def _classify_rule_based(filename: str, headers: list[str], rows: list[dict]) -> ClassifiedFile:
    """Simple keyword-based fallback classifier."""
    platform, file_type, confidence = _infer_from_signals(filename, headers, rows)

    return ClassifiedFile(
        filename=filename,
        platform=platform,
        file_type=file_type,
        confidence=confidence,
        notes="Rule-based classification from filename, headers, and sample rows",
        headers=headers,
    )
