"""
Excel report generator for Fynn P&L reports.

Builds an .xlsx workbook in memory with one tab per platform
plus a Company P&L tab that includes all business costs.
Returns raw bytes — caller is responsible for saving or uploading.
"""

import io
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ── Colour palette ─────────────────────────────────────────────────────────────
_DARK_BLUE  = "21497A"   # title row background
_LIGHT_BLUE = "D9E5F7"   # section header background
_WHITE      = "FFFFFF"
_NUMBER_FMT = '#,##0.00'


def _header_fill(hex_colour: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_colour)


def _write_sheet(
    ws,
    pnl: dict,
    show_business_costs: bool = True,
) -> None:
    """Write P&L data into a single worksheet."""

    rev       = pnl["revenue"]
    costs     = pnl.get("costs", {})
    profit    = pnl["profit"]
    ex        = pnl.get("exchange_rate_used", {})
    anomalies = pnl.get("anomalies", [])
    local_ref = pnl.get("local_reference", pnl.get("myr_reference", {}))  # backward compat
    cur       = pnl.get("currency", "SGD")
    src_cur   = local_ref.get("currency") or ex.get("from", "MYR")
    rate      = ex.get("rate", "N/A")
    period    = pnl.get("period", "")

    def _neg(val: float) -> float:
        return -abs(val) if val else 0

    # ── Column widths ──────────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 22

    # ── Helpers ────────────────────────────────────────────────────────────────
    def title_row(label: str, value="", row_idx: Optional[int] = None):
        r = ws.max_row + 1 if row_idx is None else row_idx
        ws.cell(r, 1, label)
        ws.cell(r, 2, value)
        for c in range(1, 4):
            cell = ws.cell(r, c)
            cell.fill = _header_fill(_DARK_BLUE)
            cell.font = Font(bold=True, color=_WHITE, size=13)
        return r

    def section_row(label: str):
        r = ws.max_row + 1
        ws.cell(r, 1, label)
        for c in range(1, 4):
            cell = ws.cell(r, c)
            cell.fill = _header_fill(_LIGHT_BLUE)
            cell.font = Font(bold=True)
        return r

    def col_header():
        r = ws.max_row + 1
        ws.cell(r, 2, f"Amount ({cur})")
        ws.cell(r, 2).font = Font(bold=True, italic=True)

    def data_row(label: str, value, bold: bool = False, number: bool = True):
        r = ws.max_row + 1
        ws.cell(r, 1, label).font = Font(bold=bold)
        c = ws.cell(r, 2, value)
        c.font = Font(bold=bold)
        if number and isinstance(value, (int, float)):
            c.number_format = _NUMBER_FMT
        return r

    def blank():
        ws.max_row  # just advance implicitly via append
        ws.append([""])

    # ── Title block ────────────────────────────────────────────────────────────
    title_row("Fynn Bookkeeping Report")
    data_row("Period", period, number=False)
    data_row("Platform", pnl.get("platform", ""), number=False)
    data_row("Currency", cur, number=False)
    data_row(f"{src_cur} → {cur} Rate", rate, number=False)
    generated = pnl.get("generated_at", "")
    data_row("Generated", generated[:10] if generated else "", number=False)
    blank()

    # ── Orders summary ─────────────────────────────────────────────────────────
    section_row("Orders Summary")
    col_header()
    data_row("Total Orders",  pnl.get("order_count", 0),  number=False)
    data_row("Refund Orders", pnl.get("refund_count", 0), number=False)
    blank()

    # ── Revenue ────────────────────────────────────────────────────────────────
    section_row("Revenue")
    col_header()
    data_row("Gross Sales",  rev["gross_sales"])
    data_row("Refunds",      _neg(rev["refunds"]))
    data_row("Net Revenue",  rev["net_revenue"], bold=True)
    blank()

    # ── Platform costs ─────────────────────────────────────────────────────────
    section_row("Platform Costs")
    col_header()
    data_row("Commission & Fees", _neg(costs.get("platform_fees", 0)))
    data_row("Shipping",          _neg(costs.get("shipping", 0)))
    data_row("Vouchers",          _neg(costs.get("vouchers", 0)))
    blank()

    # ── Business costs (Company P&L tab only) ──────────────────────────────────
    if show_business_costs:
        section_row("Business Costs")
        col_header()
        data_row("COGS (Supplier)", _neg(costs.get("cogs", 0)))
        data_row("Ads Spend",       _neg(costs.get("ads", 0)))
        data_row("Warehouse / 3PL", _neg(costs.get("warehouse", 0)))
        data_row("Payroll",         _neg(costs.get("payroll", 0)))
        data_row("Packaging",       _neg(costs.get("packaging", 0)))
        data_row("Other Expenses",  _neg(costs.get("other_expense", 0)))
        blank()

    total_costs_val = (
        costs.get("total_costs", 0) if show_business_costs
        else costs.get("total_platform_costs", 0)
    )
    data_row("Total Costs", _neg(total_costs_val), bold=True)
    blank()

    # ── Profit ─────────────────────────────────────────────────────────────────
    section_row("Profit")
    col_header()
    data_row("Net Profit",    profit["net_profit"],        bold=True)
    data_row("Profit Margin", f"{profit['profit_margin_pct']}%", number=False)
    blank()

    # ── Local Reference ────────────────────────────────────────────────────────
    section_row(f"{src_cur} Reference (pre-conversion)")
    r = ws.max_row + 1
    ws.cell(r, 2, f"Amount ({src_cur})").font = Font(bold=True, italic=True)
    data_row("Gross Sales",     local_ref.get("gross_sales", 0))
    data_row("Net Revenue",     local_ref.get("net_revenue", 0))
    data_row("Expected Payout", local_ref.get("expected_payout", 0))
    data_row("Actual Payout",   local_ref.get("actual_payout", 0))
    data_row("Discrepancy",     local_ref.get("discrepancy", 0))
    blank()

    # ── Anomalies ──────────────────────────────────────────────────────────────
    section_row("Anomalies")
    if anomalies:
        for a in anomalies:
            data_row(f"⚠ {a['type']} [{a['severity']}]", a["description"], number=False)
    else:
        data_row("✓ No anomalies detected", "", number=False)


def generate_summary(monthly_breakdown: list[dict], combined: dict) -> bytes:
    """
    Build a lightweight consolidated summary workbook.

    One row per (platform × period) entry — stays small regardless of how many
    months of data accumulate. No transaction detail is included.

    Args:
        monthly_breakdown: List of per-(platform × period) summary dicts from _build_combined_pnl.
        combined:          Combined P&L dict (totals row).

    Returns:
        Raw .xlsx bytes.
    """
    cur = combined.get("currency", "SGD")
    wb  = Workbook()
    ws  = wb.active
    ws.title = "Consolidated Summary"

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 12
    ws.column_dimensions["G"].width = 10

    # Title
    ws.merge_cells("A1:G1")
    title_cell = ws["A1"]
    title_cell.value = f"Fynn — Consolidated Report  |  {combined.get('period', '')}  |  {cur}"
    title_cell.font  = Font(bold=True, color=_WHITE, size=13)
    title_cell.fill  = _header_fill(_DARK_BLUE)
    title_cell.alignment = Alignment(horizontal="left")

    # Column headers
    headers = ["Period", "Platform", f"Net Revenue ({cur})", f"Total Costs ({cur})", f"Net Profit ({cur})", "Margin %", "Orders"]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(ws.max_row, col)
        cell.font = Font(bold=True, color=_WHITE)
        cell.fill = _header_fill(_DARK_BLUE)

    # Data rows
    for row in monthly_breakdown:
        ws.append([
            row.get("period", ""),
            row.get("platform", ""),
            row.get("net_revenue", 0),
            row.get("total_costs", 0),
            row.get("net_profit", 0),
            f"{row.get('margin_pct', 0):.1f}%",
            row.get("order_count", 0),
        ])
        data_row_idx = ws.max_row
        for col in [3, 4, 5]:
            ws.cell(data_row_idx, col).number_format = _NUMBER_FMT

    # Totals row
    rev = combined["revenue"]
    pft = combined["profit"]
    ws.append([
        "TOTAL", "All Platforms",
        rev["net_revenue"],
        combined["costs"]["total_costs"],
        pft["net_profit"],
        f"{pft['profit_margin_pct']:.1f}%",
        combined.get("order_count", 0),
    ])
    total_row = ws.max_row
    for col in range(1, 8):
        cell = ws.cell(total_row, col)
        cell.font = Font(bold=True, color=_WHITE)
        cell.fill = _header_fill(_DARK_BLUE)
    for col in [3, 4, 5]:
        ws.cell(total_row, col).number_format = _NUMBER_FMT

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate(pnl_reports: list[dict], combined: dict) -> bytes:
    """
    Build a workbook with one tab per platform + one Company P&L tab.

    Args:
        pnl_reports: List of per-platform P&L dicts (platform fees only).
        combined:    Combined P&L dict including all business costs.

    Returns:
        Raw .xlsx bytes.
    """
    wb = Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    # Per-platform tabs
    for pnl in pnl_reports:
        platform = pnl.get("platform", "Platform")
        period   = pnl.get("period", "")
        tab_name = f"{platform} — {period}"[:31]  # Excel tab name limit
        ws = wb.create_sheet(title=tab_name)
        _write_sheet(ws, pnl, show_business_costs=False)

    # Company P&L tab
    period     = combined.get("period", "")
    company_ws = wb.create_sheet(title=f"Company P&L — {period}"[:31])
    _write_sheet(company_ws, combined, show_business_costs=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
