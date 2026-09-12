# Real-shaped settlement files — 2026-01

Synthetic data in the shape of real exports. These are the files Fynn's parser
is actually built against; `../test_upload_pack/` holds the small canonical and
deliberately-broken ones.

## Provenance and what is verified

`LAZADA_PROVENANCE.md` carries the Lazada file's sources in full. In summary:

| | Verified | Not verified |
|---|---|---|
| Shopee | Fee names, the wide order-level structure, the reconciliation identity | Column order, header casing, CSV vs XLSX, whether adjustments are a separate file or a section, per-country columns |
| Lazada | Fee names, fee classifications, the long transaction-line structure | Same list |

**Correction — "Grand Total" is not the payout.** `LAZADA_PROVENANCE.md` calls
Grand Total the target, and the figure below is labelled that way. Lazada Seller
Center's own order detail shows otherwise: on its worked example, Subtotal 853.59
plus shipping 99.00 gives **Grand Total 952.59**, while the transaction lines for
the same order — item price credit, commission, payment fee, both shipping legs —
net to **519.18**. Grand Total is what the customer paid; the payout is what the
transaction lines sum to. The 5,816.66 below is the latter, which is the right
figure to reconcile against, under the wrong name.

Rates are illustrative. Because the second column is unverified, the parser does
not depend on any of it — see the module docstring in `services/csv_parser.py`
for how each item is accommodated, and "Tolerances proven" below.

## The two structures

**Shopee is wide.** One row per order, fees as columns, `Final Amount` stated
per row. 120 orders, 22 columns → 1,075 settlement lines, 17 distinct labels.

**Lazada is long.** One row per fee line, each carrying a Fee Classification and
a Fee Name. 501 lines, 90 orders, 25 distinct labels, 14 classifications.

The same engine cannot parse both without separate melts, which is why
`services/csv_parser.py` detects the layout rather than assuming one.

## Known totals — the regression baseline

| File | Layout | Lines | Total |
|---|---|---|---|
| `shopee-income-statement-2026-01.csv` | wide | 1,075 | 6,009.99 |
| `shopee-order-adjustments-2026-01.csv` | long | 4 | −6.24 |
| `lazada-account-statement-2026-01.csv` | long | 501 | 5,816.66 |

Reported payouts: **Shopee 6,003.75** (statement plus adjustments), **Lazada
5,816.66** (its Grand Total). Loaded together they make one 2026-01 cycle of
1,580 lines.

| | Exceptions |
|---|---|
| Before the starter pack | 46 |
| With `STARTER_RULES` (platform mechanics) | **24** |
| Month 2, after the firm's 24 decisions | **4** |

Approving the 24 ties both platforms to 0.00 with balanced journals. The four
that survive into month 2 are the three orphaned refunds — per-order judgements
that should never become rules — and Lazada's catch-all fee, which is refused a
rule deliberately.

## What each file plants

Shopee: 6 cancelled orders carrying a refund and a reverse shipping fee; an AMS
commission that only appears on some orders; withholding tax on some and not
others; an adjustments file whose last row refers to an order settled in a
previous period and so has no matching sale here.

Lazada (from its own README): 5 full refunds and 2 partial ones, each reversing
item price, commission and vouchers as *separate rows*; fee corrections running
in both directions with nearly identical names; claims that are income, not
sales; lines with no order number at all; an explicit `Other` catch-all, because
the taxonomy is open-ended by design.

## Tolerances proven

Run against the Shopee statement, each of these produces byte-identical results
(1,075 lines, 6,009.99):

- columns shuffled into a random order
- headers upper-cased, and lower-cased
- the same content as `.xlsx`
- the adjustments file appended as a second block under its own header
  (1,079 lines, 6,003.75)

Two bugs were found this way and both are now regression-guarded by the row
identity check: an over-broad `"buyer"` needle that swallowed *Shipping Fee Paid
by Buyer*, and a `"shop"` alias that read *Shopee Discount* as a platform column.
