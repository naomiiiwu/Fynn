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

On the real January files this is the whole argument in one number: 1,580
settlement lines raise **46** exceptions the first month and **3** the second.
The three that remain are orphaned refunds, which are per-order judgements and
should never become rules.

**An exception is per label, not per line.** Shopee's January statement carries
114 separate "Transaction Fee" lines. Raising each one would bury the accountant
in identical decisions, so unrecognised labels are grouped: one label, one
decision, one rule covering every line under it. Orphaned refunds stay per-line,
because the question there is about a specific order.

**A lookup is not a judgement.** A commission is an expense; an item price is
revenue; a reversal of an item price is a sales return. Fee names like these
have one defensible treatment, so Fynn ships them as a starter pack of
platform-scoped rules (`STARTER_RULES` in `services/classification.py`) rather
than making every firm decide them on day one. On the real January files that is
the difference between 46 exceptions and 24.

What is deliberately *not* in the pack is anything a firm could reasonably book
two ways — and that turns out to be most of what remains:

| Left to the firm | Because |
|---|---|
| Seller vouchers, discounts, coins, campaign and AMS fees | Marketing expense, or a reduction of revenue under IFRS 15's "consideration payable to a customer". Firms genuinely split. |
| Service and withholding taxes | Recoverable input tax or an expense, depending on registration and jurisdiction. |
| Claims and compensation | Other income, or an offset against the loss being compensated. |
| Seller balance adjustments | The clearing account, or income and expense. |

Starter rules are marked `decided_by: "Fynn starter pack"`, and every cycle
records in its audit trail how many lines they classified — those lines post
without anyone reviewing them, so the working paper must not imply the firm
approved treatments it never saw. Any of them can be overridden by approving
differently once.

**A rule can key on the platform's own classification.** Lazada files every line
under a Fee Classification — `Orders-Marketing Fees`, `Refunds-Logistics`, and
about twenty more covering some eighty fee names, a list it keeps adding to.
Approving with `scope: "category"` saves the decision against the classification
instead of the single fee name, so the other fees under that heading are covered
now and any new ones are covered the first time they appear. A decision about a
named fee always beats a decision about its classification, so a firm can widen
once and then carve out exceptions.

Category rules take their side from each line's sign, because a classification
holds both charges and their reversals — a fixed side would put a credit in the
debit column and unbalance the entry.

**Not every classification can carry a decision.** Lazada's published taxonomy
is transcribed in `data/lazada_taxonomy.py` — 21 classifications over 99 terms,
each mapped to one of BigSeller's profit buckets — and which classifications are
safe to widen is *derived* from it rather than asserted. A classification whose
terms land in more than one bucket cannot carry a single treatment:
`Orders-Lazada Fees` spans six, `Orders-Sales` three. Widening across one would
post unrelated fees to the same account, so Fynn refuses — the rule narrows back
to the named fee and the audit trail says why.

Bucket spread alone is not enough, because the source sometimes files plainly
different fees under one bucket. `Refunds-Marketing Fees` sits entirely in
Discount Promotion yet holds reversals of Seller Picks commission, DPP service
fees and item charges beside the voucher reversals. Those are named explicitly
in `ACCOUNT_HETEROGENEOUS`, with the reason, rather than inferred.

`Orders-Logistics`, `3P Services-Logistics`, `Refunds-Logistics`,
`Orders-Marketing Fees`, `Refunds-Claims` and the rest widen normally.

None are shipped pre-decided, for the same reason. The classification is offered
as a way to apply the firm's *own* decision widely, never as Fynn's guess.

Shopee publishes no equivalent — its statement is wide, and the columns are the
fee names — so category rules are Lazada-only for now.

**Some labels must never become a rule.** Lazada ships an explicit catch-all for
fees outside its own taxonomy ("The fee that does not belong to the terms
above"). What arrives under it differs every cycle, so a rule there would post
next month's unknown charge to last month's account unseen. Approving one is
recorded as a decision but writes no rule, and the label comes back next month —
by design.

**The report is the surface, conversation is for exceptions.** Accountants who
compared a structured report with a chat interface preferred the report for
reviewing and verifying entries, and wanted conversation reserved for the items
that actually need explaining. So the cycle is a page — evidence, amounts and
the resulting journal visible at once — and the only conversational surface is
the drawer that opens on a single exception.

**One workspace, no accounts.** A deployment is one firm. Every store already
takes a firm id, so supporting several firms is an authentication problem rather
than an engine one, and there is no point inventing half of one now.

---

## Project structure

```
fynn/
├── main.py                       FastAPI endpoints
├── static/app.html               The web app — cycle, upload, rules, settings
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
│   └── database.py               Supabase persistence (no-ops when unset)
├── agents/
│   ├── investigator.py           LLM exception investigation (suggestions only)
│   └── explainer.py              Conversational drawer — explains, never decides
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

Then open <http://localhost:8000>.

Locally no password is needed. In production one is: set `FYNN_PASSWORD` and the
whole app sits behind it. With it unset Fynn serves localhost only and refuses
every other caller with an explanatory 503, so deploying without it produces a
locked app rather than an open one. `/health` stays public so a platform health
check still works.

A browser gets a sign-in page and a signed cookie lasting 14 days, so the
password is typed once rather than on every visit. Scripts get an HTTP Basic
challenge instead and keep working with `curl -u fynn:…`. Changing
`FYNN_PASSWORD` invalidates every session already issued, which is the only
revocation a shared password can offer.

This is a shared password, not an accounts system. Every visitor is the same
workspace, and the audit trail names whoever the firm put in Settings — not
whoever typed the password.

No API keys are needed. Without `ANTHROPIC_API_KEY` the investigator returns no
suggestion and the exception reaches the accountant unannotated — a valid state,
not a failure. Without `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` every write
silently no-ops and the app runs on in-process state.

Everything the app does is also available to scripts:

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

Canonical columns are `platform,cycle,order,label,amount,date`, but nothing
exports that. Two real structures are handled:

- **long** — one row per fee line, carrying its own name and amount. Lazada's
  account statement; Shopee's order adjustments.
- **wide** — one row per order, fees as columns, with a stated total. Shopee's
  income statement. Each row is melted into one line per non-zero fee column.

The layout is detected, not assumed. So are the things that vary and that nobody
has verified against a live export: columns are found by name rather than
position, matching ignores case, `.xlsx` parses as well as `.csv`, adjustments
work as their own file *or* as a second block of rows under their own header,
and in a wide file any column that is not recognised meta is treated as a fee —
so an extra tax column in another market becomes a line instead of vanishing.

A settlement file *is* a period, so the cycle comes from an explicit statement
column first, then the filename, and only then a row's own date. Rows dated into
the next month are normal in a statement and must not split the close in two.
Lazada states that period as a range — `14 Dec 2020 - 20 Dec 2020` — because it
settles weekly rather than monthly, so a period is named by where it starts.

**Amounts are read in the file's own convention.** Indonesia and Vietnam write
`2.861` for two thousand eight hundred and sixty-one; Malaysia and Singapore
write `2.861` for two point eight six one. Fynn decides which from evidence in
the file — a value carrying both separators settles it outright, otherwise a
separator followed by one or two digits is a decimal point and one followed by
exactly three digits is a thousands group, since money is written to at most two
places. Guessing wrong understates an Indonesian file by a factor of a thousand,
and on a long-format file nothing downstream would catch it.

The one thing a wide file is checked against is its own arithmetic: every row's
components must sum to its stated total. A file that fails that is rejected
rather than reconciled, because a mis-read column resurfaces later as a residual
nobody can explain. That check has already caught two parser bugs.

A file it cannot read is rejected outright. Half-loading a settlement file would
produce a cycle that is short by an unknown amount, which is worse than an error.

`data/real_samples/` holds real-shaped Shopee and Lazada exports with their
provenance and known totals; `data/test_upload_pack/` holds small canonical and
deliberately-broken files, two of which are supposed to fail.

---

## The app

Open `/`. Four tabs over one cycle.

**Cycle** — payouts per platform across the top, then anything that needs a
decision. Clicking an exception opens a drawer: the first message is
deterministic, and follow-up questions go to `agents/explainer.py`, which is
given that one exception and its cycle and told in its system prompt that it
explains and proposes but never decides. Approving is a separate, explicit
action, recorded against the name in Settings rather than against whoever
happens to be at the keyboard. Where a fee carries a classification, a checkbox
offers to widen the decision across it — greyed out in practice for the
classifications that hold unrelated fees. Journal entries expand to their lines;
the audit trail sits below with the working paper one click away. When nothing
is open, **Post entries** sends them to the configured adapter.

**Upload** — drag in CSV or XLSX exports. Files are ingested one at a time,
because each folds into the same open cycle and concurrent writes would race.

**Rules** — everything the firm has accumulated, with what Fynn supplied marked
separately from what the firm decided.

**Accounts** — Fynn's account names mapped onto the firm's own chart of accounts.
A journal line names an account; Xero wants its code and QuickBooks wants its Id,
and both come from the firm's chart. Two firms use different codes for the same
idea, so Fynn does not guess — and a live ledger refuses to post while any
account a cycle touches is unmapped, naming the ones that are missing. Dry run
posts anyway and lists the gaps, which is what a dry run is for. The mapping is
per ledger: a Xero code is not a QuickBooks Id, so switching starts a fresh one.

**Settings** — firm name, who signs off, marketplaces, destination ledger, and a
**Storage** panel saying whether anything is actually being persisted. Every
database write is deliberately best-effort so a close keeps running through an
outage, which also means a misconfiguration is invisible; that panel is how you
tell the difference, and `GET /api/diagnostics` returns the same thing.

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

Environment variables are listed in `.env.example`. `FYNN_PASSWORD` is the one
that matters: without it a deployment refuses all non-local traffic.

Supabase schema lives in `migrations/`. Run `005_reconciliation.sql` then
`007_workspace_identity.sql`; `006_drop_legacy.sql` documents removing the tables
left behind by the earlier seller-facing P&L product and is commented out on
purpose.

---

## Not built yet

- Platform OAuth. Shopee, Lazada and TikTok Shop all expose finance APIs, and
  Lazada requires partner-program approval for production rate limits.
- Ledger posting. Both adapters build a correct payload against the firm's
  mapped chart of accounts — Xero as a DRAFT manual journal, QuickBooks as a
  JournalEntry with a PostingType per line — but neither makes the HTTP call.
  Each needs its app registered, OAuth 2.0 consent, and a refresh-token flow;
  Xero access tokens last 30 minutes. Refresh tokens must outlive a redeploy, so
  this wants persistence working first. Posting also needs to be idempotent
  before it goes live: pressing Post twice must not put two journals in a
  client's books.
- Accounts. A deployment is one firm's workspace behind a shared password.
  Several firms on one instance, or knowing which person approved something
  rather than which firm, both need real authentication.
- Cycle persistence. Firm rules, source files and posted entries are in
  Supabase; the *open* cycle is in-process, so a restart mid-review loses the
  approvals not yet posted. Source settlement files must be retained for the
  statutory period (5 years — verify for your jurisdiction).
- Multi-currency. Lazada alone spans six countries.
- Multi-client. A workspace holds one open cycle; separating clients within a
  firm is the next structural change, not a field.

---

## Status

Pre-product. Validated through conversations with practising accountants in
Singapore; the reconciliation engine runs end to end on sample data and accepts
real exports, through a web interface with no authentication. Not connected to
live platforms or ledgers.
