# Test upload pack

Files to throw at the Upload tab or `POST /cycle/upload`. Each
one exercises a different part of the ingest path — the point is to find where
it breaks, so two of them are meant to fail.

| File | What it tests | Expected result |
|---|---|---|
| `01_lazada_canonical.csv` | The documented shape: `platform,cycle,order,label,amount,date` | 6 lines, one orphan refund exception (`#4521`) |
| `02_shopee_native_headers.csv` | A platform's own header names, platform inferred from the filename | 5 lines, "Creator commission" raised as an unknown label |
| `03_tiktok_payout_row.csv` | Payout stated on a row inside the file, no `reported` argument needed | 5 lines, withheld balance exception, payout read as 612.00 |
| `04_credit_debit_columns.csv` | Separate credit/debit columns instead of one signed amount | 4 lines, ties out |
| `98_no_platform.csv` | No platform column, filename names no platform | Rejected: every row skipped, "No settlement lines could be read" |
| `99_unsupported_bank_statement.csv` | A bank statement, not a settlement report | Rejected: missing required column `label` |

Reported payouts, where a file does not state one:

```
01_lazada_canonical.csv        Lazada=1038.00
02_shopee_native_headers.csv   Shopee=725.00
04_credit_debit_columns.csv    Lazada=760.00
```

## Seller Center export shapes

`05_lazada_seller_center_export.csv` carries the exact column set Lazada Seller
Center shows under Finance → Account Statements → **Transaction Overview →
Export**:

```
Transaction Date | Transaction Type | Transaction Number | Order Number
Order Item ID | Item Name | Comment | Amount | Statement Period
```

Two things it establishes:

- **Statement Period is a range** — `2022-01-12 - 2022-01-19`. Lazada settles
  weekly, so a cycle is not a calendar month. Fynn names the period by where it
  starts, which is a patch over an assumption, not a fix for it.
- **There is no Fee Classification column.** BigSeller's documentation lists one
  because BigSeller reads Lazada's API; this export does not carry it. Category
  rules (`scope: "category"`) therefore do nothing on a file exported this way —
  they need the API, or an export variant that includes the column.

The `Comment` column is captured onto the line and passed to the investigator
and the explainer. On an adjustment it usually holds the reason.
