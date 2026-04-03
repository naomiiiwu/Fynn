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
        """Initialise with credentials path and spreadsheet ID from environment."""
        self.credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
        self.client: Optional[gspread.Client] = None

    def _authenticate(self) -> bool:
        """
        Authenticate using the service account JSON key file.

        Returns:
            True if authentication succeeded, False otherwise.
        """
        if not self.credentials_path:
            print("  [Sheets] GOOGLE_SHEETS_CREDENTIALS_PATH not set — skipping Sheets output.")
            return False

        if not os.path.exists(self.credentials_path):
            print(f"  [Sheets] Credentials file not found: {self.credentials_path}")
            return False

        try:
            creds = Credentials.from_service_account_file(self.credentials_path, scopes=SCOPES)
            self.client = gspread.authorize(creds)
            print("  [Sheets] Authenticated successfully with service account.")
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

    def _apply_formatting(self, sheet: gspread.Worksheet) -> None:
        """
        Apply bold formatting to header rows and currency formatting to data cells.

        Args:
            sheet: The worksheet to format.
        """
        try:
            # Main title — dark blue bg, white bold text
            sheet.format("A1:C1", {
                "backgroundColor": {"red": 0.13, "green": 0.29, "blue": 0.53},
                "textFormat": {
                    "bold": True,
                    "fontSize": 13,
                    "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                },
            })

            # Section header rows: Orders(8), Revenue(13), Costs(19), Profit(26), MYR Ref(31), Anomalies(39)
            section_rows = [8, 13, 19, 26, 31, 39]
            for row in section_rows:
                sheet.format(f"A{row}:C{row}", {
                    "backgroundColor": {"red": 0.85, "green": 0.91, "blue": 0.98},
                    "textFormat": {"bold": True},
                })

            # Total rows bold: Net Revenue(17), Total Costs(25), Net Profit(28)
            for row in [17, 25, 28]:
                sheet.format(f"A{row}:B{row}", {"textFormat": {"bold": True}})

            # Column widths via batchUpdate
            spreadsheet = sheet.spreadsheet
            sheet_id = sheet.id
            spreadsheet.batch_update({"requests": [
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 220},
                    "fields": "pixelSize",
                }},
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                    "properties": {"pixelSize": 160},
                    "fields": "pixelSize",
                }},
            ]})

            print("  [Sheets] Formatting applied.")
        except Exception as exc:
            print(f"  [Sheets] Formatting failed (non-critical): {exc}")

    def write_pnl(self, pnl: dict) -> bool:
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
        sheet_title = f"Fynn - {period}"
        sheet = self._get_or_create_sheet(sheet_title)

        if sheet is None:
            return False

        rev = pnl["revenue"]
        costs = pnl["costs"]
        profit = pnl["profit"]
        ex = pnl.get("exchange_rate_used", {})
        anomalies = pnl.get("anomalies", [])
        myr_ref = pnl.get("myr_reference", {})

        cur = pnl["currency"]
        rate = ex.get("rate", "N/A")

        rows = [
            # Title block
            ["Fynn Bookkeeping Report", "", ""],
            ["Period", period, ""],
            ["Platform", pnl["platform"], ""],
            ["Currency", cur, ""],
            ["MYR → SGD Rate", rate, ""],
            ["Generated", pnl["generated_at"][:10], ""],
            ["", "", ""],

            # Orders summary
            ["Orders Summary", "", ""],
            ["", "Count", ""],
            ["Total Orders", pnl.get("order_count", 0), ""],
            ["Refund Orders", pnl.get("refund_count", 0), ""],
            ["", "", ""],

            # Revenue
            ["Revenue", "", ""],
            ["", f"Amount ({cur})", ""],
            ["Gross Sales", rev["gross_sales"], ""],
            ["Refunds", -rev["refunds"], ""],
            ["Net Revenue", rev["net_revenue"], ""],
            ["", "", ""],

            # Costs
            ["Costs", "", ""],
            ["", f"Amount ({cur})", ""],
            ["Platform Fees", -costs["platform_fees"], ""],
            ["Shipping", -costs["shipping"], ""],
            ["Vouchers", -costs["vouchers"], ""],
            ["Total Costs", -costs["total_costs"], ""],
            ["", "", ""],

            # Profit
            ["Profit", "", ""],
            ["", f"Amount ({cur})", ""],
            ["Net Profit", profit["net_profit"], ""],
            ["Profit Margin", f"{profit['profit_margin_pct']}%", ""],
            ["", "", ""],

            # MYR Reference
            ["MYR Reference (pre-conversion)", "", ""],
            ["", "Amount (MYR)", ""],
            ["Gross Sales", myr_ref.get("gross_sales", 0), ""],
            ["Net Revenue", myr_ref.get("net_revenue", 0), ""],
            ["Expected Payout", myr_ref.get("expected_payout", 0), ""],
            ["Actual Payout", myr_ref.get("actual_payout", 0), ""],
            ["Discrepancy", myr_ref.get("discrepancy", 0), ""],
            ["", "", ""],

            # Anomalies
            ["Anomalies", "", ""],
        ]

        if anomalies:
            for a in anomalies:
                rows.append([f"⚠️ {a['type']} [{a['severity']}]", a["description"], ""])
        else:
            rows.append(["✅ No anomalies detected", "", ""])

        try:
            sheet.update("A1", rows, value_input_option="USER_ENTERED")
            self._apply_formatting(sheet)
            url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            print(f"  [Sheets] P&L written successfully → {url}")
            return True
        except Exception as exc:
            print(f"  [Sheets] Failed to write data: {exc}")
            return False
