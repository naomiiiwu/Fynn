# Fynn — settlement reconciliation for Southeast Asian marketplaces

Fynn reconciles payouts from Shopee, Lazada and TikTok Shop and posts clean,
traceable journal entries into Xero or QuickBooks. It is built for accounting
firms, not sellers.

A2X solves this for Amazon and Shopify. It does not cover the platforms that
dominate Southeast Asia. The tools that do exist here (Zetpy, SiteGiant, SQL
Account) sync orders into the ledger but leave reconciliation and exception
handling to the firm — which is the part that actually costs time.

---

## What it does

When a seller is paid by a marketplace, the amount landing in the bank is not
the amount they sold. A $1,200 sale becomes a $1,038 payout after commission,
vouchers and refunds — sometimes including refunds from a previous cycle.
Reconstructing that chain is manual work; one Singapore firm quoted roughly two
working days per settlement cycle.

Fynn:

1. Classifies every settlement line against the firm's own rules
2. Checks the classified total against the payout the platform reported
3. Investigates anything that does not tie out, and proposes evidence
4. Waits for the accountant to approve — nothing posts unreviewed
5. Saves each decision as a rule, so the same exception is not raised twice
6. Builds one balanced, summarised journal entry per platform
7. Records every step with a timestamp and a named actor

---

## Design decisions worth knowing

**The LLM is confined to exceptions.** Classifying a known fee label is a
lookup with one correct answer, so it runs deterministically in
`services/classification.py`. The LLM in `agents/investigator.py` only handles
labels never seen before and residuals that need explaining. It always returns a
suggestion with a confidence score, never a decision. This is what practising
accountants said they require, and it also keeps token cost near zero at volume.

**Fynn does not touch bank data.** It checks the classified total against the
payout figure in the settlement file itself. Matching the actual bank deposit
happens inside Xero/QuickBooks, using the bank feed already connected there.
Building that here would duplicate existing infrastructure.

**One entry per platform, not one per transaction.** Posting 45 separate entries
would clutter the ledger. The clearing account carries the net so the ledger's
own reconciliation can match the deposit when it arrives.

**Rules are the accumulating asset.** Every approval writes a rule scoped to the
firm, applied across all their clients. After a year, a firm's rule set encodes
its own accounting policy — which is why switching costs something.

**The digest page is the review surface; WhatsApp is the doorbell.** Accountants
who compared a structured report with a chat interface preferred the report for
reviewing and verifying entries, and wanted conversation reserved for the items
that actually need explaining. So the link arrives over WhatsApp, and the review
happens on a page where the evidence, the amounts and the resulting journal are
visible at once. Approving is not a WhatsApp command — an approval is recorded
against a named person and should not be a two-word reply.

**The link is the credential.** A digest link is tokenised, scoped to one firm,
and expires in 14 days. No login, because accountants already receive everything
else from their clients this way and a password would just be one more thing to
lose. A token that has expired or was never issued is a hard 410 — it never
falls through to somebody else's cycle.

---

## Project structure

```
fynn/
├── main.py                       FastAPI endpoints + Twilio WhatsApp webhook
├── static/digest.html            The month-end digest page + exception drawer
├── models/
│   ├── transaction.py            Settlement lines, rules, exceptions, journals
│   └── firm_profile.py           Per-firm settings + profile store
├── services/
│   ├── classification.py         Deterministic rules engine + firm rule store
│   ├── reconciliation.py         Residual detection + 5 exception strategies
│   ├── journal.py                Balanced summarised entry construction
│   ├── ledger.py                 Pluggable adapters (dry-run, Xero)
│   ├── audit.py                  Audit trail + working paper export
│   ├── csv_parser.py             Real-world settlement exports → SettlementLine
│   ├── conversation.py           WhatsApp command grammar
│   ├── whatsapp.py               Twilio delivery
│   └── database.py               Supabase persistence (no-ops when unset)
├── agents/
│   ├── investigator.py           LLM exception investigation (suggestions only)
│   ├── explainer.py              Conversational drawer — explains, never decides
│   └── notifier.py               Outbound WhatsApp sends
├── utils/formatter.py            Cycle orchestration + digest payload
├── data/sample_settlements.py    Sample cycle with the three known edge cases
├── data/test_upload_pack/        Files to break the ingest path with
└── migrations/                   Supabase schema (005 is the current one)
```

---

## Running it

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

No API keys are needed. Without `ANTHROPIC_API_KEY` the investigator returns no
suggestion and the exception reaches the accountant unannotated — a valid state,
not a failure. Without `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` every write
silently no-ops and the app runs on in-process state. Without Twilio credentials
outbound messages print to the console.

```bash
# Load the sample cycle
curl -X POST localhost:8000/cycle/sample

# Approve an exception (key comes from the digest)
curl -X POST localhost:8000/approve \
  -H "Content-Type: application/json" \
  -d '{"key":"Lazada|2026-01|Refund|#4521","account":"Sales Returns & Allowances","side":"debit","actor":"D. Tan"}'

# Try to post — refuses while exceptions remain
curl -X POST localhost:8000/post

# Working paper
curl localhost:8000/audit
```

### Uploading a real settlement file

```bash
curl -X POST localhost:8000/cycle/upload \
  -F "file=@data/test_upload_pack/01_lazada_canonical.csv" \
  -F "reported=Lazada=1038.00"
```

Canonical columns: `platform,cycle,order,label,amount,date`. The parser also
accepts a marketplace's own header names (`Transaction Date`, `Order SN`,
`Transaction Type`, `Amount (MYR)`, separate `Credit`/`Debit` columns), infers
the platform from the filename when there is no platform column, and reads the
payout from a `Total payout` / `Settlement` / `Withdrawal` row when the file
states one — in which case `reported` can be omitted.

A file it cannot read is rejected outright. Half-loading a settlement file would
produce a cycle that is short by an unknown amount, which is worse than an error.

Expect this to break on the first real file — unfamiliar labels, different
column names, a payout that does not tie. Each break is a finding.
`data/test_upload_pack/` has six files, two of which are supposed to fail.

---

## The digest page

The review surface. Reached by a tokenised link at `/c/{token}` — no login, 14
day expiry, scoped to one firm.

- Payouts per platform across the top, then anything that needs a decision.
- Clicking an exception opens a drawer. The first message is deterministic; the
  follow-up questions go to `agents/explainer.py`, which is given that one
  exception and its cycle and told, in the system prompt, that it explains and
  proposes but never decides. Approving is a separate, explicit action.
- Approving records the account against the name in firm settings — the request
  body cannot name the actor, because a page with no login cannot be trusted to
  say who is holding the phone.
- Journal entries expand to their lines, so any figure can be traced back.
- Once nothing is open, **Post entries** sends them to the configured adapter.
- The audit trail is on the same page, with the working paper one click away.

## WhatsApp

Twilio posts to `POST /webhook/whatsapp`. It carries files in and links out:

| Send | What happens |
|---|---|
| a settlement CSV | Reconciled in the background; the digest link comes back |
| `digest` | A fresh link to the current cycle |
| `setup` | Link to the firm settings page |
| anything else | The digest link, which is nearly always what was wanted |

`POST /notify` issues a link and sends it out of band, for when a cycle is
prepared some other way.

Firms who prefer a browser get two more pages: `/setup?phone=…` for settings and
`/upload?phone=…` for drag-and-drop ingest. An upload through the web page still
sends the digest link to WhatsApp, so the surfaces cannot drift apart.

---

## Sample cycle

Three platforms, 16 lines, three exceptions drawn from what accountants
identified as the hard cases:

| Platform | Issue | Residual |
|---|---|---|
| Lazada | Refund references an order from the previous cycle | 12.00 |
| Shopee | "Creator commission" — no rule exists for this label | 8.40 |
| TikTok Shop | Withheld balance — settled but not released | 96.00 |

Resolve all three and every platform ties to zero with balanced journals.

---

## Deployment

Railway, from the repo root:

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT   # Procfile
python-3.12                                          # runtime.txt
```

Environment variables are listed in `.env.example`. Point the Twilio WhatsApp
sandbox (or an approved sender) at `https://<your-domain>/webhook/whatsapp`, and
set `RAILWAY_PUBLIC_DOMAIN` so the setup and upload links Fynn sends resolve.

Supabase schema lives in `migrations/`. `005_reconciliation.sql` is the current
one; `006_drop_legacy.sql` documents removing the tables left behind by the
earlier seller-facing P&L product and is commented out on purpose.

---

## Not built yet

- Platform OAuth. Shopee, Lazada and TikTok Shop all expose finance APIs, and
  Lazada requires partner-program approval for production rate limits.
- Xero posting. The adapter prepares a DRAFT manual journal payload but the HTTP
  call is not wired up — OAuth 2.0 with `accounting.transactions` scope and a
  refresh-token flow are needed first. Access tokens expire after 30 minutes.
- Cycle persistence. Firm rules, source files and posted entries are in
  Supabase; the *open* cycle is in-process, so a restart mid-review loses the
  approvals not yet posted. Digest tokens live in the same process, so a restart
  also invalidates every link already sent — both want the same fix. Source
  settlement files must be retained for the statutory period (5 years — verify
  for your jurisdiction).
- Multi-currency. Lazada alone spans six countries.
- Multi-client. A firm is one WhatsApp number and one open cycle; separating
  clients within a firm is the next structural change, not a field.

---

## Status

Pre-product. Validated through conversations with practising accountants in
Singapore; the reconciliation engine runs end to end on sample data, accepts
real CSVs, and is reachable over WhatsApp. Not connected to live platforms or
ledgers.
