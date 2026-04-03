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
        self.credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "")
        self.spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
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
            # Bold rows 1, 5, 11, 17, 22 (section headers and totals)
            bold_rows = [1, 2, 6, 12, 18, 23, 28]
            for row in bold_rows:
                sheet.format(f"A{row}:C{row}", {"textFormat": {"bold": True}})

            # Light blue fill for main header
            sheet.format("A1:C1", {
                "backgroundColor": {"red": 0.2, "green": 0.5, "blue": 0.8},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            })

            # Light grey fill for section headers
            for row in [6, 12, 18, 23]:
                sheet.format(f"A{row}:C{row}", {
                    "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                    "textFormat": {"bold": True},
                })

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
            # Title
            ["Fynn Bookkeeping Report", "", ""],
            [f"Period: {period}", f"Platform: {pnl['platform']}", f"Generated: {pnl['generated_at'][:10]}"],
            [f"Currency: {cur}", f"MYR→SGD Rate: {rate}", f"Source: {ex.get('source', 'N/A')}"],
            ["", "", ""],

            # Orders summary
            ["Orders Summary", "", ""],
            ["Metric", "Count", ""],
            ["Total Orders", pnl.get("order_count", 0), ""],
            ["Refund Orders", pnl.get("refund_count", 0), ""],
            ["", "", ""],

            # Revenue
            ["Revenue", "", ""],
            ["Line Item", f"Amount ({cur})", ""],
            ["Gross Sales", f"{cur} {rev['gross_sales']:,.2f}", ""],
            ["Refunds", f"-{cur} {rev['refunds']:,.2f}", ""],
            ["Net Revenue", f"{cur} {rev['net_revenue']:,.2f}", ""],
            ["", "", ""],

            # Costs
            ["Costs", "", ""],
            ["Line Item", f"Amount ({cur})", ""],
            ["Platform Fees", f"-{cur} {costs['platform_fees']:,.2f}", ""],
            ["Shipping", f"-{cur} {costs['shipping']:,.2f}", ""],
            ["Vouchers", f"-{cur} {costs['vouchers']:,.2f}", ""],
            ["Total Costs", f"-{cur} {costs['total_costs']:,.2f}", ""],
            ["", "", ""],

            # Profit
            ["Profit", "", ""],
            ["Line Item", f"Amount ({cur})", ""],
            ["Net Profit", f"{cur} {profit['net_profit']:,.2f}", ""],
            ["Profit Margin", f"{profit['profit_margin_pct']}%", ""],
            ["", "", ""],

            # MYR Reference
            ["MYR Reference (Pre-conversion)", "", ""],
            ["Gross Sales (MYR)", f"MYR {myr_ref.get('gross_sales', 0):,.2f}", ""],
            ["Net Revenue (MYR)", f"MYR {myr_ref.get('net_revenue', 0):,.2f}", ""],
            ["Expected Payout (MYR)", f"MYR {myr_ref.get('expected_payout', 0):,.2f}", ""],
            ["Actual Payout (MYR)", f"MYR {myr_ref.get('actual_payout', 0):,.2f}", ""],
            ["Discrepancy (MYR)", f"MYR {myr_ref.get('discrepancy', 0):,.2f}", ""],
            ["", "", ""],

            # Anomalies
            ["Anomalies", "", ""],
        ]

        if anomalies:
            for a in anomalies:
                rows.append([f"[{a['severity']}] {a['type']}", a["description"], ""])
        else:
            rows.append(["✅ No anomalies detected", "", ""])

        try:
            sheet.update("A1", rows)
            self._apply_formatting(sheet)
            url = f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            print(f"  [Sheets] P&L written successfully → {url}")
            return True
        except Exception as exc:
            print(f"  [Sheets] Failed to write data: {exc}")
            return False
