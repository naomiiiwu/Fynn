"""
Google Sheets output service.

Authenticates with a service account, creates or updates a sheet
named "Fynn - March 2026", and writes the P&L in a clean layout
with bold headers and currency formatting.
"""

import os
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsService:
    """Handles Google Sheets creation and data writing for P&L reports."""

    def __init__(self) -> None:
        """Initialise with credentials and spreadsheet ID from environment."""
        self.credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
        self.credentials_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
        self.client: Optional[gspread.Client] = None

    def _authenticate(self) -> bool:
        """
        Authenticate using either:
          1. GOOGLE_SHEETS_CREDENTIALS_JSON env var (Railway/production)
          2. GOOGLE_SHEETS_CREDENTIALS_PATH file path (local dev)

        Returns:
            True if authentication succeeded, False otherwise.
        """
        import json

        try:
            import signal, threading

            def _auth(result: list) -> None:
                try:
                    if self.credentials_json:
                        try:
                            info = json.loads(self.credentials_json)
                        except Exception:
                            print("  [Sheets] GOOGLE_SHEETS_CREDENTIALS_JSON is not valid JSON — check Railway env vars.")
                            result.append(("error", "invalid JSON"))
                            return
                        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
                        self.client = gspread.authorize(creds)
                        result.append(("json", True))
                    elif self.credentials_path and os.path.exists(self.credentials_path):
                        creds = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
                        self.client = gspread.authorize(creds)
                        result.append(("file", True))
                    else:
                        result.append(("missing", False))
                except Exception as exc:
                    result.append(("error", exc))

            result: list = []
            t = threading.Thread(target=_auth, args=(result,), daemon=True)
            t.start()
            t.join(timeout=15)  # 15-second cap on auth

            if not result:
                print("  [Sheets] Authentication timed out after 15s — skipping Sheets.")
                return False

            source, outcome = result[0]
            if source == "missing":
                print("  [Sheets] No credentials found — set GOOGLE_SHEETS_CREDENTIALS_JSON or GOOGLE_SHEETS_CREDENTIALS_PATH.")
                return False
            if source == "error":
                print(f"  [Sheets] Authentication failed: {outcome}")
                return False

            print(f"  [Sheets] Authenticated via {'env JSON' if source == 'json' else 'credentials file'}.")
            return True

        except Exception as exc:
            print(f"  [Sheets] Authentication failed: {exc}")
            return False

    def _get_or_create_sheet(self, title: str) -> Optional[gspread.Worksheet]:
        """
        Get an existing worksheet by title, or create it if it doesn't exist.

        Args:
            title: The worksheet tab name (e.g. "Fynn - March 2026").

        Returns:
            The gspread Worksheet object, or None on failure.
        """
        try:
            spreadsheet = self.client.open_by_key(self.spreadsheet_id)
            try:
                sheet = spreadsheet.worksheet(title)
                sheet.clear()
                print(f"  [Sheets] Cleared existing sheet: '{title}'")
            except gspread.WorksheetNotFound:
                sheet = spreadsheet.add_worksheet(title=title, rows=60, cols=10)
                print(f"  [Sheets] Created new sheet: '{title}'")
            return sheet
        except Exception as exc:
            print(f"  [Sheets] Failed to get/create sheet: {exc}")
            return None

    def _apply_formatting(self, sheet: gspread.Worksheet, anomaly_start_row: int) -> None:
        """Apply formatting: title, section headers, bold totals, column widths."""
        try:
            sheet.format("A1:C1", {
                "backgroundColor": {"red": 0.13, "green": 0.29, "blue": 0.53},
                "textFormat": {
                    "bold": True, "fontSize": 13,
                    "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                },
            })

            # Layout (fixed rows — see write_pnl for row map):
            # 8=Orders, 13=Revenue, 19=Platform Costs, 25=Business Costs, 36=Profit, 41=MYR Ref, anomaly_start_row=Anomalies
            section_rows = [8, 13, 19, 25, 36, 41, anomaly_start_row]
            for row in section_rows:
                sheet.format(f"A{row}:C{row}", {
                    "backgroundColor": {"red": 0.85, "green": 0.91, "blue": 0.98},
                    "textFormat": {"bold": True},
                })

            # Bold totals: Net Revenue(17), Total Costs(34), Net Profit(38)
            for row in [17, 34, 38]:
                sheet.format(f"A{row}:B{row}", {"textFormat": {"bold": True}})

            spreadsheet = sheet.spreadsheet
            sheet_id = sheet.id
            spreadsheet.batch_update({"requests": [
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 230}, "fields": "pixelSize",
                }},
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                    "properties": {"pixelSize": 160}, "fields": "pixelSize",
                }},
            ]})
            print("  [Sheets] Formatting applied.")
        except Exception as exc:
            print(f"  [Sheets] Formatting failed (non-critical): {exc}")

    def write_pnl(self, pnl: dict, tab_title: str | None = None) -> bool:
        """
        Write the full P&L report to Google Sheets.

        Args:
            pnl: The P&L dict produced by formatter.generate_pnl().

        Returns:
            True if the write succeeded, False otherwise.
        """
        if not self._authenticate():
            return False

        period = pnl.get("period", "Unknown Period")
        sheet_title = tab_title or f"Fynn - {period}"
        sheet = self._get_or_create_sheet(sheet_title)

        if sheet is None:
            return False

        rev     = pnl["revenue"]
        costs   = pnl["costs"]
        profit  = pnl["profit"]
        ex      = pnl.get("exchange_rate_used", {})
        anomalies = pnl.get("anomalies", [])
        myr_ref = pnl.get("myr_reference", {})
        cur     = pnl.get("currency", "SGD")
        rate    = ex.get("rate", "N/A")

        def _neg(val: float) -> float:
            """Show costs as negative numbers so they read as deductions."""
            return -abs(val) if val else 0

        # ── Row map (1-indexed, must match _apply_formatting section_rows) ──
        # 1-7:   Title block
        # 8-12:  Orders summary
        # 13-18: Revenue
        # 19-24: Platform costs
        # 25-35: Business costs
        # 36-40: Profit
        # 41-48: MYR reference
        # 49+:   Anomalies
        rows = [
            # ── Title block (rows 1-7) ──────────────────────────────────────
            ["Fynn Bookkeeping Report", "", ""],              # 1
            ["Period", period, ""],                           # 2
            ["Platform", pnl.get("platform", ""), ""],       # 3
            ["Currency", cur, ""],                            # 4
            [f"MYR → {cur} Rate", rate, ""],                 # 5
            ["Generated", pnl.get("generated_at", "")[:10], ""],  # 6
            ["", "", ""],                                     # 7

            # ── Orders summary (rows 8-12) ──────────────────────────────────
            ["Orders Summary", "", ""],                       # 8  ← section
            ["", "Count", ""],                                # 9
            ["Total Orders", pnl.get("order_count", 0), ""], # 10
            ["Refund Orders", pnl.get("refund_count", 0), ""],# 11
            ["", "", ""],                                     # 12

            # ── Revenue (rows 13-18) ─────────────────────────────────────────
            ["Revenue", "", ""],                              # 13 ← section
            ["", f"Amount ({cur})", ""],                      # 14
            ["Gross Sales", rev["gross_sales"], ""],           # 15
            ["Refunds", _neg(rev["refunds"]), ""],            # 16
            ["Net Revenue", rev["net_revenue"], ""],           # 17 ← bold
            ["", "", ""],                                     # 18

            # ── Platform costs (rows 19-24) ──────────────────────────────────
            ["Platform Costs", "", ""],                       # 19 ← section
            ["", f"Amount ({cur})", ""],                      # 20
            ["Commission & Fees", _neg(costs.get("platform_fees", 0)), ""],  # 21
            ["Shipping", _neg(costs.get("shipping", 0)), ""], # 22
            ["Vouchers", _neg(costs.get("vouchers", 0)), ""], # 23
            ["", "", ""],                                     # 24

            # ── Business costs (rows 25-35) ──────────────────────────────────
            ["Business Costs", "", ""],                       # 25 ← section
            ["", f"Amount ({cur})", ""],                      # 26
            ["COGS (Supplier)", _neg(costs.get("cogs", 0)), ""],          # 27
            ["Ads Spend", _neg(costs.get("ads", 0)), ""],                 # 28
            ["Warehouse / 3PL", _neg(costs.get("warehouse", 0)), ""],     # 29
            ["Payroll", _neg(costs.get("payroll", 0)), ""],               # 30
            ["Packaging", _neg(costs.get("packaging", 0)), ""],           # 31
            ["Other Expenses", _neg(costs.get("other_expense", 0)), ""],  # 32
            ["", "", ""],                                     # 33
            ["Total Costs", _neg(costs.get("total_costs", 0)), ""],       # 34 ← bold
            ["", "", ""],                                     # 35

            # ── Profit (rows 36-40) ──────────────────────────────────────────
            ["Profit", "", ""],                               # 36 ← section
            ["", f"Amount ({cur})", ""],                      # 37
            ["Net Profit", profit["net_profit"], ""],          # 38 ← bold
            ["Profit Margin", f"{profit['profit_margin_pct']}%", ""],  # 39
            ["", "", ""],                                     # 40

            # ── MYR Reference (rows 41-48) ───────────────────────────────────
            ["MYR Reference (pre-conversion)", "", ""],       # 41 ← section
            ["", "Amount (MYR)", ""],                         # 42
            ["Gross Sales", myr_ref.get("gross_sales", 0), ""],   # 43
            ["Net Revenue", myr_ref.get("net_revenue", 0), ""],   # 44
            ["Expected Payout", myr_ref.get("expected_payout", 0), ""],  # 45
            ["Actual Payout", myr_ref.get("actual_payout", 0), ""],      # 46
            ["Discrepancy", myr_ref.get("discrepancy", 0), ""],          # 47
            ["", "", ""],                                     # 48

            # ── Anomalies (row 49+) ──────────────────────────────────────────
            ["Anomalies", "", ""],                            # 49 ← section
        ]

        anomaly_start_row = 49
        if anomalies:
            for a in anomalies:
                rows.append([f"⚠️ {a['type']} [{a['severity']}]", a["description"], ""])
        else:
            rows.append(["✅ No anomalies detected", "", ""])

        try:
            sheet.update("A1", rows, value_input_option="USER_ENTERED")
            self._apply_formatting(sheet, anomaly_start_row)
            url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            print(f"  [Sheets] P&L written successfully → {url}")
            return True
        except Exception as exc:
            print(f"  [Sheets] Failed to write data: {exc}")
            return False
