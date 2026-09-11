# Synthetic Lazada account statement — 2026-01

## Provenance

Fee names and classifications come from BigSeller's "Profit Analysis for Lazada",
which lists the Lazada Fee Classification and the exact Terms in Lazada for every
fee, and states the data is obtained from the Lazada API. Retrieved September 2026.

<https://help.bigseller.com/en_US/detailPage/10/1/2786/content>

**Verified:** fee names, fee classifications, the long transaction-line structure,
and that Grand Total is Lazada's term for order revenue.

**Not verified:** column order, header casing, file format, encoding, per-country
variation. Rates here are illustrative.

## Structure differs from Shopee

Shopee is **wide** — one row per order, fees as columns. Lazada is **long** — one
row per fee line, each carrying a Fee Classification and a Fee Name. The same
engine cannot parse both without separate adapters.

## Totals

| | |
|---|---|
| Lines | 501 |
| Orders | 90 |
| **Grand Total** | **5,816.66** |

Fee classifications present: 14

- `Orders-Lazada Fees` — 205 lines
- `Orders-Logistics` — 119 lines
- `Orders-Sales` — 90 lines
- `Orders-Marketing Fees` — 46 lines
- `Reimbursements` — 9 lines
- `Refunds-Lazada Fees` — 9 lines
- `Refunds-Sales` — 8 lines
- `Refunds-Logistics` — 5 lines
- `Refunds-Marketing Fees` — 3 lines
- `Refunds-Claims` — 2 lines
- `Adjustments` — 2 lines
- `Orders-Claims` — 1 lines
- `Other` — 1 lines
- `3P Services-Logistics` — 1 lines

## What is planted

- 5 full refunds — each reverses item price, commission and vouchers as separate lines
- 2 partial refunds — reversals are proportional, not full
- fee corrections run BOTH ways — 'refund - correction for overcharge' is a credit, 'correction for undercharge' is a debit. Same fee, opposite signs.
- claims (Lost, Damaged, Shipping Fee) are income — miscoding them as sales overstates revenue
- seller balance adjustments and storage fees carry no order number
- order 789114520 settled in 2025-12 — its reversal lands here with no matching sale
- Lazada itself has an 'Other' catch-all for fees outside its taxonomy — the list is open-ended by design

## Why this one is harder than Shopee

**Reversals are separate rows, not adjusted values.** A refund produces Reversal
Item Price, Reversal Commission, Reversal Promotional Charges Vouchers and Return
shipping fees — four rows, in a different classification from the original. An
engine that expects a refund to be one line will not tie out.

**Corrections run both ways.** "Commission fee refund - correction for overcharge"
is a credit. "Commission fee - correction for undercharge" is a debit. Same fee,
opposite direction, nearly identical names.

**Claims are income.** Lost Claim, Damaged Claim and Shipping Fee Claims are
compensation. Coding them as sales overstates revenue and distorts the commission
base.

**Some lines have no order.** Balance adjustments, storage fees and corrections
carry no order number. Matching on order ID alone will drop them.

**The taxonomy is open-ended.** Lazada has an explicit "Other" bucket for fees
outside its own list. Any rule set will go stale.
