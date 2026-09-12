"""Generate two sample settlement exports, in the platforms' own shapes.

Deterministic: a fixed seed, so the totals in README.md stay true and the files
can serve as a regression baseline. Run with `python data/sample_exports/generate.py`.

Structure, and how confident we are in it:

  Lazada   Exactly the nine columns Seller Center shows under Finance →
           Account Statements → Transaction Overview → Export, read off the
           BigSeller screenshot. Statement Period is a weekly range, and there
           is no Fee Classification column — that one comes from the API.
           Transaction Types are drawn only from the 99 verified terms in
           data/lazada_taxonomy.py.

  Shopee   The wide order-level income statement: one row per order, fees as
           columns, components summing to Final Amount. The fee names are the
           ones BigSeller publishes plus the shipping and tax columns our
           existing sample carries. Shopee's exact header list is *not*
           verified the way Lazada's is — see data/sample_exports/README.md.
"""
from __future__ import annotations

import csv
import io
import pathlib
import random

HERE = pathlib.Path(__file__).parent
SEED = 20260112

PRICES = [12.90, 19.50, 24.00, 35.90, 48.00, 62.50, 89.00, 129.00, 210.00]


def _r(value: float) -> float:
    return round(value + 1e-9, 2)


# ── Shopee: wide, one row per order ──────────────────────────────────────────

SHOPEE_FEES = [
    "Product Price", "Seller Voucher", "Shopee Discount", "Shopee Coins Redeemed",
    "Shipping Fee Paid by Buyer", "Shipping Fee Rebate From Shopee",
    "Shipping Fee Borne by Seller", "Actual Shipping Fee", "Reverse Shipping Fee",
    "Commission Fee", "Transaction Fee", "Service Fee", "AMS Commission Fee",
    "Lost Compensation", "Product Service Tax (GST)", "Shipping Fee Service Tax (GST)",
    "Withholding Tax", "Refund Amount",
]
SHOPEE_HEADER = ["Order ID", "Order Creation Date", "Release Time", "Order Status"] \
    + SHOPEE_FEES + ["Final Amount"]


def shopee_rows(rng: random.Random) -> list[dict]:
    rows = []
    for i in range(60):
        day = 1 + i // 2
        price = rng.choice(PRICES)
        cancelled = i in (7, 19, 28, 41, 53)          # 5 cancelled orders
        f = {name: 0.0 for name in SHOPEE_FEES}
        f["Product Price"] = price

        if rng.random() < 0.45:
            f["Seller Voucher"] = _r(-price * rng.choice([0.05, 0.10]))
        if rng.random() < 0.30:
            f["Shopee Discount"] = _r(-price * 0.05)
        if rng.random() < 0.25:
            f["Shopee Coins Redeemed"] = _r(-price * 0.02)

        if rng.random() < 0.55:                        # buyer paid shipping
            paid = rng.choice([2.50, 3.90, 5.00])
            f["Shipping Fee Paid by Buyer"] = paid
            f["Shipping Fee Rebate From Shopee"] = _r(paid * 0.40)
            f["Shipping Fee Borne by Seller"] = _r(-paid * 1.20)
            f["Actual Shipping Fee"] = _r(-paid * 1.15)
            f["Shipping Fee Service Tax (GST)"] = _r(-paid * 0.11)

        if cancelled:
            f["Refund Amount"] = _r(-price)
            f["Reverse Shipping Fee"] = -2.50
            for fee in ("Commission Fee", "Transaction Fee", "Service Fee",
                        "AMS Commission Fee", "Product Service Tax (GST)"):
                f[fee] = 0.0
        else:
            net = price + f["Seller Voucher"] + f["Shopee Discount"]
            f["Commission Fee"] = _r(-net * 0.055)
            f["Transaction Fee"] = _r(-price * 0.0227)
            f["Service Fee"] = _r(-net * 0.021)
            if rng.random() < 0.22:                    # affiliate orders only
                f["AMS Commission Fee"] = _r(-price * 0.10)
            f["Product Service Tax (GST)"] = _r(
                (f["Commission Fee"] + f["Transaction Fee"] + f["Service Fee"]) * 0.08
            )
            if rng.random() < 0.12:
                f["Withholding Tax"] = _r(-price * 0.01)
            if rng.random() < 0.05:                    # compensation for a lost parcel
                f["Lost Compensation"] = _r(price * 0.5)

        row = {
            "Order ID": f"25{day:02d}{rng.randint(100000, 999999)}",
            "Order Creation Date": f"2026-01-{day:02d}",
            "Release Time": f"2026-01-{min(day + 12, 31):02d}",
            "Order Status": "Cancelled" if cancelled else "Completed",
        }
        row.update({k: f"{v:.2f}" for k, v in f.items()})
        row["Final Amount"] = f"{_r(sum(f.values())):.2f}"
        rows.append(row)
    return rows


SHOPEE_ADJUSTMENTS = [
    ("Lost Compensation", "2026-01-28", 58.51),
    ("Overseas Return Service Fee", "2026-01-28", -9.80),
    ("Delivery Failure Fee", "2026-01-29", -6.95),
    ("Return refund for order settled in previous period", "2026-01-09", -48.00),
]


def write_shopee() -> tuple[pathlib.Path, float, int]:
    rng = random.Random(SEED)
    rows = shopee_rows(rng)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHOPEE_HEADER)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    # The adjustments arrive as a second block under their own header, in the
    # same file — one of the two layouts platforms are known to use.
    buf.write("\n")
    adj = csv.writer(buf)
    adj.writerow(["Order ID", "Adjustment Reason", "Adjustment Date", "Released Amount"])
    for i, (reason, date, amount) in enumerate(SHOPEE_ADJUSTMENTS):
        order = rows[i * 7]["Order ID"] if i < 3 else "2512184472910"   # last one is an orphan
        adj.writerow([order, reason, date, f"{amount:.2f}"])

    path = HERE / "shopee-income-statement-2026-01.csv"
    path.write_text(buf.getvalue())
    payout = _r(sum(float(r["Final Amount"]) for r in rows)
                + sum(a[2] for a in SHOPEE_ADJUSTMENTS))
    return path, payout, len(rows)


# ── Lazada: long, one row per transaction ────────────────────────────────────

LAZADA_HEADER = ["Transaction Date", "Transaction Type", "Transaction Number",
                 "Order Number", "Order Item ID", "Item Name", "Comment",
                 "Amount", "Statement Period"]

# Weekly, as Lazada settles. The last one crosses into February, which is normal
# and must not split the January close.
PERIODS = [
    ("2026-01-05", "2026-01-11"), ("2026-01-12", "2026-01-18"),
    ("2026-01-19", "2026-01-25"), ("2026-01-26", "2026-02-01"),
]
ITEMS = ["Baby Hair Clip Set", "Poly Mailer 100pcs", "Ceramic Mug 350ml",
         "Cotton Tote Bag", "Stainless Straw Set"]


VOUCHER_TERMS = [
    "Promotional Charges Vouchers", "Seller Funded Marketing Voucher",
    "Lazcoin discount", "Promotional Charges Flexi-Combo",
    "Promotional Charges Bundles", "LCP Fee",
]


def write_lazada() -> tuple[pathlib.Path, float, int]:
    rng = random.Random(SEED + 1)
    rows: list[list] = []
    seq = iter(range(100000, 999999))
    # Every order's charges are kept, so a reversal can mirror what was actually
    # taken rather than inventing a round number. Once reversed, an order is out
    # of the pool: reversing the same sale twice would return more than was sold.
    placed: list[dict] = []
    reversed_orders: set[str] = set()

    def _statement(period: tuple[str, str]) -> str:
        # "14 Dec 2020 - 20 Dec 2020" is the only Statement Period format seen on
        # a real Lazada screen, so it is the one used here. Fynn reads the ISO
        # form equally well; see README.md on what is and is not verified.
        from datetime import date as _date
        out = []
        for value in period:
            y, m, d = (int(x) for x in value.split("-"))
            out.append(_date(y, m, d).strftime("%d %b %Y"))
        return f"{out[0]} - {out[1]}"

    def add(date, ttype, order, item_no, item, comment, amount, period):
        rows.append([date, ttype, f"MY{next(seq)}", order, item_no, item, comment,
                     f"{amount:.2f}", _statement(period)])

    for pi, period in enumerate(PERIODS):
        start_day = int(period[0][-2:])
        for _ in range(18):
            order = str(rng.randint(700000000, 799999999))
            item_no, item = f"{order}-1", rng.choice(ITEMS)
            day = f"2026-01-{min(start_day + rng.randint(0, 6), 31):02d}"
            price = rng.choice(PRICES)
            charge = {"order": order, "item_no": item_no, "item": item, "price": price,
                      "commission": _r(-price * 0.045), "payment": _r(-price * 0.0327),
                      "voucher": 0.0, "voucher_term": None, "ship": 0.0}

            add(day, "Item Price Credit", order, item_no, item, "", price, period)
            add(day, "Commission", order, item_no, item, "", charge["commission"], period)
            add(day, "Payment Fee", order, item_no, item, "", charge["payment"], period)

            if rng.random() < 0.55:
                ship = rng.choice([2.50, 3.90, 5.00])
                charge["ship"] = ship
                add(day, "Shipping Fee (Paid By Customer)", order, item_no, item, "", ship, period)
                add(day, "Shipping Fee Paid by Seller", order, item_no, item, "",
                    _r(-ship * 1.15), period)
                if rng.random() < 0.30:
                    add(day, "Shipping Fee Voucher (by Lazada)", order, item_no, item, "",
                        _r(ship * 0.5), period)

            # Most orders carry a promotion of some kind — sellers run them
            # continuously, and they are the bulk of what a firm has to classify.
            if rng.random() < 0.70:
                term = rng.choice(VOUCHER_TERMS)
                amount = _r(-price * rng.choice([0.05, 0.10]))
                charge["voucher"], charge["voucher_term"] = amount, term
                add(day, term, order, item_no, item, "", amount, period)
            # Fulfilled by a 3PL rather than by Lazada's own network. In the one
            # real order we have sight of, the 3P shipping charge was the single
            # largest deduction on the order.
            if rng.random() < 0.22:
                add(day, "Shipping Fee (Charged By 3P)", order, item_no, item, "",
                    _r(-price * rng.choice([0.18, 0.29])), period)
                if rng.random() < 0.40:
                    add(day, "FBL Handling Fee", order, item_no, item, "",
                        _r(-price * 0.03), period)
                if rng.random() < 0.25:
                    add(day, "Import Duties and Tax (Charged by 3PL)", order, item_no,
                        item, "Cross-border shipment", _r(-price * 0.06), period)
            if rng.random() < 0.20:
                add(day, "Campaign Fee", order, item_no, item, "", _r(-price * 0.02), period)
            if rng.random() < 0.10:
                add(day, "Seller Picks Commission", order, item_no, item, "",
                    _r(-price * 0.03), period)
            if rng.random() < 0.08:
                add(day, "Reimbursements Withholding tax", order, item_no, item, "",
                    _r(-price * 0.01), period)
            placed.append(charge)

        # Reversals of orders settled earlier in the file. Lazada does not write
        # a "refund" line: it reverses each original row separately, in a
        # different classification, so a full return is four or five rows.
        if pi > 0:
            eligible = [c for c in placed[: -18] if c["order"] not in reversed_orders]
            for target in rng.sample(eligible, k=min(3, len(eligible))):
                reversed_orders.add(target["order"])
                day = f"2026-01-{min(start_day + rng.randint(1, 5), 31):02d}"
                partial = rng.random() < 0.34
                share = 0.5 if partial else 1.0
                why = "Partial return accepted" if partial else "Buyer returned item"
                o, ino, it = target["order"], target["item_no"], target["item"]

                add(day, "Reversal Item Price", o, ino, it, why,
                    _r(-target["price"] * share), period)
                add(day, "Reversal Commission", o, ino, it, "",
                    _r(-target["commission"] * share), period)
                add(day, "Payment Fee Credit", o, ino, it, "",
                    _r(-target["payment"] * share), period)
                if target["voucher_term"]:
                    add(day, "Reversal Promotional Charges Vouchers", o, ino, it,
                        f"Reverses {target['voucher_term']}",
                        _r(-target["voucher"] * share), period)
                if target["ship"]:
                    add(day, "Return shipping fees", o, ino, it,
                        "Return leg charged to seller", -2.50, period)

    last = PERIODS[-1]
    # Fees with no order behind them at all.
    add("2026-01-30", "Storage Fee", "", "", "", "January warehouse storage", -18.40, last)
    add("2026-01-30", "Pick Up Fee", "", "", "", "", -4.20, last)
    add("2026-01-30", "Inventory Storage Fees", "", "", "", "FBL warehouse", -11.25, last)
    add("2026-01-31", "DPP Service Fee", "", "", "", "", -6.80, last)
    add("2026-01-31", "Seller balance adjustments - Credit", "", "", "",
        "Goodwill credit", 22.15, last)
    add("2026-01-31", "Seller balance adjustments - Debit", "", "", "", "", -7.80, last)
    add("2026-01-31", "Lazada Bonus", "", "", "", "Seller incentive programme", 15.00, last)
    add("2026-01-31", "The fee that does not belong to the terms above", "", "", "",
        "Unclassified platform charge", -5.60, last)
    # A reversal whose sale settled in December — no matching order here.
    add("2026-01-14", "Reversal Item Price", "689114520", "689114520-1", ITEMS[2],
        "Return for order settled in previous statement", -62.50, PERIODS[1])

    path = HERE / "lazada-account-statement-2026-01.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(LAZADA_HEADER)
        writer.writerows(rows)
    payout = _r(sum(float(r[7]) for r in rows))
    return path, payout, len(rows)


if __name__ == "__main__":
    sp, sp_payout, sp_orders = write_shopee()
    lz, lz_payout, lz_lines = write_lazada()
    print(f"{sp.name}: {sp_orders} orders + {len(SHOPEE_ADJUSTMENTS)} adjustments, "
          f"payout {sp_payout:,.2f}")
    print(f"{lz.name}: {lz_lines} transaction lines, payout {lz_payout:,.2f}")
