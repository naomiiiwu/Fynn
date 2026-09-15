-- Fynn — Migration 015: one row per settlement, kept for every month.
--
-- Until now a firm had one open cycle and nothing else. Uploading February
-- replaced January on the screen; January's files and posts were still stored,
-- but nothing listed them, so a past month could not be found again, and an
-- unfinished one was pushed aside without a word.
--
-- A settlement is one payout: one bank deposit, one document in Xero. That is
-- the unit A2X lists and the unit an accountant reconciles, so it is the unit
-- here. The month is still what a close is named after, and still where the
-- exceptions are decided; a settlement records which month it belongs to.
--
-- Two halves to a row:
--
--   what Fynn computes   total, lines, open_exceptions — rewritten whenever the
--                        month is reconciled again, so the list is current
--                        without rebuilding every month to draw it.
--
--   what was sent        posted_* and ledger_* — written once per post and never
--                        recomputed. A posted document is a fact about the
--                        ledger, and rebuilding it under today's rules would
--                        show something other than what the ledger holds.

create table if not exists settlements (
    firm_id           text not null references firm_profiles(workspace_id) on delete cascade,
    reference         text not null,                 -- the document number, e.g. JE-LAZ-2026-01-05JAN2026…
    cycle             text not null,                 -- the month it closes in
    platform          text not null,
    period            text not null default '',      -- the platform's own wording
    period_end        text not null default '',      -- ISO date, for sorting and dating
    total             numeric(14, 2) not null default 0,
    lines             jsonb not null default '[]'::jsonb,
    open_exceptions   integer not null default 0,
    ledger            text not null default '',      -- where it was sent: 'xero' | 'dry-run'
    ledger_id         text not null default '',      -- Xero's InvoiceID
    ledger_status     text not null default '',      -- DRAFT | AUTHORISED | PAID | VOIDED | DELETED
    document          text not null default '',      -- ACCREC | ACCPAY
    posted_total      numeric(14, 2),
    posted_lines      jsonb,
    posted_at         timestamptz,
    posted_by         text not null default '',
    status_checked_at timestamptz,
    updated_at        timestamptz not null default now(),
    primary key (firm_id, reference)
);

create index if not exists idx_settlements_firm_cycle
    on settlements (firm_id, cycle);
