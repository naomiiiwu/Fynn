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
    return _classify_rule_based(filename, headers)


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

Rules:
- platform = the e-commerce platform this data is from, or "generic" for internal cost files
- file_type:
    "transactions" = orders, refunds, fees, settlements from a platform
    "cogs"         = cost of goods / supplier invoices / purchase orders
    "ads"          = advertising / marketing spend
    "warehouse"    = storage, fulfilment, 3PL costs
    "payroll"      = staff / labour costs
    "packaging"    = packaging material costs
    "expense"      = any other business expense
- confidence = how sure you are (0.0–1.0)
- If unsure, use "unknown" and set confidence < 0.5"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # fast + cheap for classification
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text.strip())
        return ClassifiedFile(
            filename=filename,
            platform=result.get("platform", "unknown"),
            file_type=result.get("file_type", "unknown"),
            confidence=float(result.get("confidence", 0.5)),
            notes=result.get("notes", ""),
            headers=headers,
        )
    except Exception as exc:
        print(f"  [Classifier] Claude failed: {exc}, falling back to rules")
        return _classify_rule_based(filename, headers)


def _classify_rule_based(filename: str, headers: list[str]) -> ClassifiedFile:
    """Simple keyword-based fallback classifier."""
    name_lower = filename.lower()
    headers_lower = " ".join(headers).lower()
    combined = name_lower + " " + headers_lower

    # Platform detection
    platform = "unknown"
    if "shopee" in combined:
        platform = "shopee"
    elif "lazada" in combined:
        platform = "lazada"
    elif "amazon" in combined:
        platform = "amazon"
    elif "shopify" in combined:
        platform = "shopify"
    elif "tiktok" in combined:
        platform = "tiktok"

    # File type detection
    file_type = "unknown"
    if any(k in combined for k in ["transaction", "order", "settlement", "payout", "commission", "buyer payment"]):
        file_type = "transactions"
    elif any(k in combined for k in ["supplier", "purchase", "cogs", "cost of goods", "invoice", "unit cost"]):
        file_type = "cogs"
    elif any(k in combined for k in ["ads", "advertising", "marketing", "campaign", "spend", "impression"]):
        file_type = "ads"
    elif any(k in combined for k in ["warehouse", "storage", "fulfilment", "fulfillment", "3pl"]):
        file_type = "warehouse"
    elif any(k in combined for k in ["payroll", "salary", "staff", "labour", "labor", "employee"]):
        file_type = "payroll"
    elif any(k in combined for k in ["packaging", "package", "box", "poly", "mailer"]):
        file_type = "packaging"
    elif any(k in combined for k in ["expense", "cost", "fee", "overhead"]):
        file_type = "expense"

    if platform != "unknown" and file_type == "unknown":
        file_type = "transactions"  # platform file = likely transactions

    confidence = 0.7 if platform != "unknown" or file_type != "unknown" else 0.3

    return ClassifiedFile(
        filename=filename,
        platform=platform,
        file_type=file_type,
        confidence=confidence,
        notes=f"Rule-based classification (no Claude API)",
        headers=headers,
    )
