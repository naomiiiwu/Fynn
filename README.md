# Fynn — Autonomous AI Bookkeeping Agent

Fynn is an AI-powered bookkeeping agent built for cross-border e-commerce sellers. It connects to e-commerce platform APIs, pulls transaction data, reconciles payouts, handles multi-currency conversion, generates a Profit & Loss report, and delivers a weekly summary via WhatsApp — all automatically. This MVP demonstrates the full pipeline using simulated Shopee Malaysia data for one seller in March 2026.

---

## What Each Service Does

| Module | Purpose |
|---|---|
| `data/mock_shopee_data.py` | 50 orders, 5 refunds, fees, vouchers, and settlement for March 2026 |
| `services/reconciliation.py` | Computes expected payout and flags discrepancies > 2% |
| `services/currency.py` | MYR → SGD via Open Exchange Rates API (or hardcoded fallback) |
| `agents/bookkeeper.py` | Claude AI for transaction categorization and anomaly explanations |
| `utils/formatter.py` | Builds the canonical P&L dict and WhatsApp message |
| `services/sheets.py` | Writes formatted P&L to Google Sheets with bold headers |
| `services/whatsapp.py` | Sends summary via Twilio WhatsApp; falls back to console print |
| `main.py` | FastAPI app with `/health` and `/run-monthly-report` endpoints |

---

## Setup Instructions

### 1. Prerequisites

- Python 3.11+
- A Google Cloud service account with Sheets + Drive API enabled
- (Optional) Anthropic API key, Open Exchange Rates key, Twilio account

### 2. Install dependencies

```bash
cd fynn
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your keys. The minimum required to run in mock mode is:

```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_SHEETS_CREDENTIALS_PATH=../fynn-492206-860397b7f950.json
GOOGLE_SHEETS_SPREADSHEET_ID=your_spreadsheet_id_here
```

To find your Spreadsheet ID: open your Google Sheet and copy the long string from the URL between `/d/` and `/edit`.

**Important:** Share your Google Sheet with the service account email (found in the JSON key file under `"client_email"`). Give it **Editor** access.

### 4. Run the server

```bash
cd fynn
uvicorn main:app --reload --port 8000
```

---

## How to Run the MVP

### Trigger the full pipeline

```bash
curl -X POST http://localhost:8000/run-monthly-report
```

This runs all 8 steps:
1. Load mock Shopee data (50 orders, 5 refunds, fees, settlement)
2. Reconcile the payout
3. Categorize every transaction with Claude AI
4. Detect anomalies (large refunds, high fees, payout discrepancy)
5. Convert MYR → SGD
6. Generate the P&L report
7. Write to Google Sheets
8. Send WhatsApp summary (or print to console)

### Health check

```bash
curl http://localhost:8000/health
```

---

## How to Test Without Real API Keys (Mock Mode)

Fynn is designed to run gracefully without any third-party credentials:

| Service | Behaviour when key is missing |
|---|---|
| Anthropic Claude | Skips AI calls; uses direct transaction-type mapping |
| Open Exchange Rates | Uses hardcoded MYR→SGD rate (0.304) |
| Google Sheets | Saves P&L as `pnl_report.json` in the project root |
| Twilio WhatsApp | Prints the formatted message to the console |

So you can run the full pipeline end-to-end with **zero API keys** — the P&L will be computed and printed to your terminal, and saved as a JSON file.

---

## Expected Output (March 2026 Mock Data)

```
Gross Sales (MYR):   7,523.30
Refunds (MYR):         297.00
Platform Fees (MYR):   263.65
Shipping (MYR):        166.00
Vouchers (MYR):         85.00
Expected Payout (MYR): 6,711.65
Actual Payout (MYR):   6,698.45
Discrepancy:           -13.20 (-0.20%) ✅

Net Revenue (SGD):   ~2,167.62
Net Profit (SGD):    ~1,902.34
Profit Margin:       ~87.8%

Anomalies:
  ⚠️ TXN-REF-005 — Refund of MYR 120.00 is 30.8% of order ORD-2026-035 (MYR 390.00)
```

---

## Project Structure

```
fynn/
├── main.py                  # FastAPI app entry point
├── .env.example             # Environment variable template
├── requirements.txt         # All dependencies
├── data/
│   └── mock_shopee_data.py  # Mock Shopee transaction data
├── agents/
│   └── bookkeeper.py        # Claude AI categorization + anomaly detection
├── services/
│   ├── reconciliation.py    # Payout reconciliation logic
│   ├── currency.py          # Multi-currency conversion
│   ├── sheets.py            # Google Sheets P&L output
│   └── whatsapp.py          # WhatsApp delivery via Twilio
├── models/
│   └── transaction.py       # Pydantic data models
└── utils/
    └── formatter.py         # P&L generation and message formatting
```
