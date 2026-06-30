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
    """Return headers and first N data rows from CSV or Excel bytes."""
    # Excel workbook: ZIP magic bytes PK\x03\x04
    if content[:4] == b"PK\x03\x04":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            rows_raw = list(ws.iter_rows(values_only=True))
            wb.close()
            if not rows_raw:
                return [], []
            headers = [str(c).strip() if c is not None else "" for c in rows_raw[0]]
            rows = []
            for row in rows_raw[1: max_rows + 1]:
                if any(c is not None for c in row):
                    rows.append({headers[j]: row[j] for j in range(min(len(headers), len(row)))})
            return headers, rows
        except Exception:
            return [], []

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
    # Most-specific signals first — warehouse before packaging because 3PL invoices
    # often list "Bubble Wrap Insert" as a line-item service, which must not override
    # the stronger 3PL/Pick-and-Pack column signals.
    if any(token in combined for token in ["payroll", "basic pay", "epf", "socso", "salary", "employee", "headcount", "labour", "labor"]):
        file_type = "payroll"
    elif any(token in combined for token in ["pick and pack", "3pl", "3pl provider", "fulfilment", "fulfillment", "inbound receiving", "outbound labelling", "outbound"]):
        file_type = "warehouse"
    elif any(token in combined for token in ["warehouse", "storage fee", "storage cost", "monthly storage"]):
        file_type = "warehouse"
    elif any(token in combined for token in ["poly mailer", "bubble wrap", "desiccant", "packing material", "poly bag", "thank you card"]):
        file_type = "packaging"
    elif any(token in combined for token in ["buyer payment", "order income", "shopee commission", "lazada commission", "referral fee", "gross sales", "net income", "payout", "settlement", "withdrawal", "bank transfer"]):
        file_type = "transactions"
    elif any(token in combined for token in ["transaction", "refund", "commission"]) and platform != "unknown":
        file_type = "transactions"
    elif any(token in combined for token in ["roas", "cpc", "cpm", "ad spend", "impression", "shopee ads", "lazada ads", "sponsored"]):
        file_type = "ads"
    elif any(token in combined for token in ["ads", "advertising", "campaign", "click"]):
        file_type = "ads"
    elif any(token in combined for token in ["unit cost", "cost of goods", "cogs", "sku cost", "product cost", "purchase order"]):
        file_type = "cogs"
    elif any(token in combined for token in ["supplier", "invoice", "purchase"]):
        file_type = "cogs"
    elif any(token in combined for token in ["packaging", "carton", "mailer", "tape"]):
        file_type = "packaging"
    elif any(token in combined for token in ["overhead", "utilities", "subscription", "miscellaneous", "rental"]):
        file_type = "expense"
    elif any(token in combined for token in ["expense"]):
        file_type = "expense"

    if platform != "unknown" and file_type == "unknown":
        file_type = "transactions"

    # Cost files are platform-agnostic — override any spurious platform match
    _cost_types = {"cogs", "ads", "warehouse", "payroll", "packaging", "expense"}
    if file_type in _cost_types:
        platform = "generic"

    if platform != "unknown" and file_type != "unknown":
        confidence = 0.95
    elif platform != "unknown" or file_type != "unknown":
        confidence = 0.85
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

STEP 1 — Identify file_type first (most important):
- "transactions" = platform finance export: orders, refunds, commissions, payouts, settlements, withdrawals
- "cogs"         = supplier/product costs: columns like Supplier, Invoice No., SKU, Unit Cost, Quantity, Total Cost, Product Name, Purchase Order
- "ads"          = advertising: columns like Campaign, Impressions, Clicks, Ad Spend, ROAS, CPC, CPM
- "warehouse"    = fulfilment/storage: columns like 3PL, Pick and Pack, Storage, Inbound, Outbound, Fulfilment
- "payroll"      = staff costs: columns like Employee, Salary, Basic Pay, EPF, SOCSO, Allowance, Headcount
- "packaging"    = packing materials: columns like Poly Mailer, Bubble Wrap, Carton, Tape, Desiccant, Packing Material
- "expense"      = other overhead: columns like Category, Description, Vendor, Subscription, Utilities, Rental, Miscellaneous

STEP 2 — Identify platform (only matters for "transactions" files):
- "shopee"  = "Shopee Commission", "Buyer Payment", "shopee" in filename or data
- "lazada"  = "Lazada", "LazWallet", "LazPay" anywhere
- "amazon"  = "Amazon", "ASIN", "FBA" anywhere
- "shopify" = "Shopify", "Shop Pay" anywhere
- "tiktok"  = "TikTok", "TikShop" anywhere
- "generic" = internal cost file with no platform branding — USE THIS for cogs/payroll/warehouse/packaging/expense/ads files
- "unknown" = truly cannot determine

CRITICAL RULES:
- If file_type is cogs/payroll/warehouse/packaging/expense/ads → set platform to "generic", NOT "unknown"
- "Supplier Name", "Invoice No.", "Unit Cost", "SKU", "Quantity" columns = cogs, confidence >= 0.92
- "Employee", "Basic Pay", "EPF", "SOCSO" columns = payroll, confidence >= 0.95
- "3PL Provider", "Pick and Pack", "Storage", "Inbound", "Outbound", "Fulfilment" columns = warehouse, confidence >= 0.95
- "Poly Mailer", "Bubble Wrap", "Carton", "Desiccant" columns = packaging, confidence >= 0.95
- IMPORTANT: A 3PL/warehouse invoice may list "Bubble Wrap" or "Poly Mailer" as a SERVICE LINE ITEM inside a Service Type column — this does NOT make it a packaging file. If the file has a "3PL Provider" or "Service Type" column with warehouse services, it is "warehouse" even if bubble wrap appears as one line item.

Be decisive. Strong column signals → confidence >= 0.88. Only use "unknown" if genuinely unreadable."""

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
