Fynn WhatsApp Test Upload Pack

Use these CSVs to test the current WhatsApp onboarding and reconciliation flow.

Suggested setup:
- Marketplaces: Shopee, Lazada
- Required supporting files: COGS, Ads spend

Suggested test order:
1. Send `01_shopee_finance_march_v1.csv`.
   - Expected: saved, but Fynn asks for missing Lazada, COGS, and Ads before reconciliation.
2. Send `02_lazada_finance_march_v1.csv`.
   - Expected: saved, but Fynn asks for missing COGS and Ads. Reply `proceed` to test partial reconciliation, or keep sending files.
3. Send `03_cogs_march.csv` and `04_ads_march.csv`.
   - Expected: once required files are present, Fynn refreshes reconciliation automatically.
4. Send `05_shopee_finance_march_v2.csv`.
   - Expected: replaces the Shopee transaction data and refreshes the report again.
5. Send `99_unsupported_bank_statement.csv`.
   - Expected: rejected or held for clarification because it does not match an accepted Fynn template.

All files are intentionally small so they are easy to inspect in WhatsApp tests.
