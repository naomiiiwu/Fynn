# Test upload pack

Files to throw at `/cycle/upload`, `/upload`, or straight into WhatsApp. Each
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
